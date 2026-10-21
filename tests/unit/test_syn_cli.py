"""The installed SYN catalog requires neither a database nor a model provider."""

import json
import subprocess
import sys

from potbot.cli import main


def test_catalog_offline_and_includes_agent_workflow_aliases(capsys) -> None:
    assert main(["syn", "catalog", "--summary"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["native_type_count"] == 529
    assert summary["data_type_alias_count"] == 47
    assert summary["selected_count"] == 576
    for alias in ("dt:agent.definition", "dt:workflow.run"):
        assert main(["syn", "catalog", "--type-ref", alias]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["selected_count"] == 1
        assert json.loads(result["types"][0]["metadata_json"])["data_type"]["id"] == alias
    assert main(["syn", "catalog", "--type-ref", "syn://type/absent-native-class"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "invalid_input"


def test_catalog_type_schemes_are_equivalent_without_duplicate_definitions(capsys) -> None:
    for slug, canonical in (
        ("person", "syn://type/person"),
        ("potbot-data-file-agents-md", "synny://type/potbot-data-file-agents-md"),
    ):
        definitions = []
        for scheme in ("syn", "synny"):
            assert main(["syn", "catalog", "--type-ref", f"{scheme}://type/{slug}"]) == 0
            selected = json.loads(capsys.readouterr().out)
            assert selected["selected_count"] == 1
            assert selected["types"][0]["ref"] == canonical
            definitions.append(selected["types"][0])
        assert definitions[0] == definitions[1]


def test_catalog_process_cannot_import_network_database_or_model_adapters(tmp_path) -> None:
    program = """
import importlib.abc
import socket
import sys
blocked = {'psycopg', 'sqlalchemy', 'transformers', 'torch', 'requests', 'httpx', 'syn'}
class DenyAdapters(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in blocked:
            raise AssertionError('unexpected adapter: ' + fullname)
def deny_network(*args, **kwargs):
    raise AssertionError('unexpected network')
sys.meta_path.insert(0, DenyAdapters())
socket.socket.connect = deny_network
socket.create_connection = deny_network
from potbot.cli import main
assert main(['syn', 'catalog', '--summary']) == 0
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["selected_count"] == 576
