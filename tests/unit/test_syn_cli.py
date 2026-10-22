"""The installed SYN catalog requires neither a database nor a model provider."""

import json
import subprocess
import sys
from uuid import uuid4

import pytest

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


def test_hierarchy_and_placement_run_without_database_model_or_network(tmp_path) -> None:
    program = """
import importlib.abc
import os
import socket
import sys
os.environ.pop('POTBOT_DSN', None)
os.environ.pop('POTBOT_TEST_DSN', None)
blocked = {'psycopg', 'sqlalchemy', 'transformers', 'torch', 'requests', 'httpx', 'syn'}
class DenyAdapters(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in blocked or fullname.startswith(
            ('potbot.persistence', 'potbot.memory')
        ):
            raise AssertionError('unexpected adapter: ' + fullname)
def deny_network(*args, **kwargs):
    raise AssertionError('unexpected network')
sys.meta_path.insert(0, DenyAdapters())
socket.socket.connect = deny_network
socket.create_connection = deny_network
from potbot.cli import main
assert main(['syn', 'hierarchy', '--summary']) == 0
assert main([
    'syn', 'placement', '--type-ref', 'synny://type/agent-process', '--check-kind', 'cog'
]) == 0
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    summary, placement = [json.loads(line) for line in result.stdout.splitlines()]
    assert summary["authored_type_count"] == 114
    assert summary["effective_type_count"] == 690
    assert "types" not in summary
    assert placement["decision"] == "matches_recommendation"
    assert placement["type"]["placement"]["preferred_kind"] == "cog"
    assert placement["enforces_import_placement"] is False


def test_hierarchy_cli_filters_and_exact_type_url_errors(capsys, monkeypatch) -> None:
    monkeypatch.delenv("POTBOT_DSN", raising=False)
    assert main(["syn", "hierarchy", "--primitive-kind", "cog", "--limit", "2"]) == 0
    selected = json.loads(capsys.readouterr().out)
    assert selected["returned_count"] == len(selected["types"]) == 2
    assert all(row["placement"]["preferred_kind"] == "cog" for row in selected["types"])
    assert selected["primitive_filter_semantics"] == "explicit_advisory_recommendation"
    for command in (
        ["hierarchy", "--include-subtypes"],
        ["hierarchy", "--limit", "0"],
        ["placement", "--type-ref", "dt:agent.run"],
        ["placement", "--type-ref", "agent-process"],
    ):
        assert main(["syn", *command]) == 2
        assert json.loads(capsys.readouterr().err)["error"] == "invalid_input"


@pytest.mark.parametrize("command", ["list", "search"])
def test_primitive_kind_cli_forwards_instance_filters(command, capsys, monkeypatch) -> None:
    from potbot.memory import embeddings as embeddings_module
    from potbot.memory import local_encoder
    from potbot.persistence import artifacts as artifacts_module
    from potbot.persistence import database as database_module
    from potbot.semantics import service as service_module

    recorded = []

    class Service:
        def __init__(self, *args):
            pass

        def list_objects(self, *args, **kwargs):
            recorded.append(("list", args, kwargs))
            return {"ok": True}

        def search(self, *args, **kwargs):
            recorded.append(("search", args, kwargs))
            return {"ok": True}

    monkeypatch.setenv("POTBOT_DSN", "postgresql://unused:unused@127.0.0.1:1/unused")
    monkeypatch.setattr(database_module, "Database", lambda *args, **kwargs: object())
    monkeypatch.setattr(artifacts_module, "ArtifactStore", lambda *args: object())
    monkeypatch.setattr(artifacts_module, "LocalArtifactBackend", lambda *args: object())
    monkeypatch.setattr(service_module, "SynService", Service)
    monkeypatch.setattr(embeddings_module, "EmbeddingService", lambda *args: object())
    monkeypatch.setattr(local_encoder, "QwenLocalEncoder", lambda *args: object())
    workspace, principal, snapshot, episode, space = (uuid4() for _ in range(5))
    arguments = [
        "syn",
        command,
        "--workspace-id",
        str(workspace),
        "--principal-id",
        str(principal),
        "--snapshot-id",
        str(snapshot),
        "--primitive-kind",
        "event",
        "--type-ref",
        "synny://type/agent-execution",
        "--include-subtypes",
    ]
    if command == "search":
        arguments.extend(
            [
                "--episode-id",
                str(episode),
                "--space-id",
                str(space),
                "--embedding-name",
                "semantic_text",
                "--query",
                "find executions",
            ]
        )
    assert main(arguments) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    called, positional, filters = recorded.pop()
    assert not recorded and called == command
    assert positional[0].workspace_id == workspace
    assert positional[0].principal_id == principal
    assert positional[1] == snapshot
    assert filters["primitive_kind"] == "event"
    assert filters["type_ref"] == "synny://type/agent-execution"
    assert filters["include_subtypes"] is True
    if command == "search":
        assert positional[2] == "find executions"
        assert positional[4:] == (episode, space)
        assert filters["embedding_name"] == "semantic_text"


def test_primitive_cli_rejects_type_search_before_database_configuration(
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.delenv("POTBOT_DSN", raising=False)
    arguments = [
        "syn",
        "search",
        "--workspace-id",
        str(uuid4()),
        "--principal-id",
        str(uuid4()),
        "--snapshot-id",
        str(uuid4()),
        "--episode-id",
        str(uuid4()),
        "--space-id",
        str(uuid4()),
        "--query",
        "types",
        "--record-kind",
        "types",
        "--primitive-kind",
        "event",
    ]
    assert main(arguments) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "invalid_input"
    assert "primitive-kind" in error["message"]
