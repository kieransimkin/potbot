"""Installation, dispatch, and dependency isolation checks."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from potbot import __version__


def run_cli(*arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "potbot", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_installed_cli_works_outside_source_directory(tmp_path: Path) -> None:
    version = run_cli("--version", cwd=tmp_path)
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip() == f"potbot {__version__}"
    help_result = run_cli("--help", cwd=tmp_path)
    assert help_result.returncode == 0, help_result.stderr
    assert {"fixture", "db", "info"}.issubset(set(help_result.stdout.split()))
    info = run_cli("info", cwd=tmp_path)
    assert info.returncode == 0, info.stderr
    payload = json.loads(info.stdout)
    assert payload["implementation_phase"] == 2
    assert payload["model_inference_available"] is False
    assert payload["model_downloads_required"] is False


def test_nested_help_does_not_require_database_or_model_services(tmp_path: Path) -> None:
    for command in ("db", "fixture", "admin", "task", "worker", "embedding", "syn"):
        result = run_cli(command, "--help", cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout


def test_domain_import_cannot_load_optional_adapters_or_open_network(tmp_path: Path) -> None:
    program = """
import importlib.abc
import socket
import sys

blocked = {"psycopg", "openai", "anthropic", "transformers", "torch", "httpx", "requests"}
class DenyAdapters(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split(".")[0] in blocked:
            raise AssertionError("domain imported optional adapter: " + fullname)

def deny_network(*args, **kwargs):
    raise AssertionError("domain attempted a network connection")

sys.meta_path.insert(0, DenyAdapters())
socket.socket.connect = deny_network
socket.create_connection = deny_network
import potbot.domain
assert not blocked.intersection(sys.modules)
print("isolated")
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "isolated"


def test_unknown_command_fails(tmp_path: Path) -> None:
    assert run_cli("solve", cwd=tmp_path).returncode == 2
