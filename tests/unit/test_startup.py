"""The shared launcher entry point prepares only the capabilities it needs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from potbot import cli, credentials, startup
from potbot.domain.errors import SourceUnavailableError


def test_explicit_commands_are_forwarded_without_postgres_or_model_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[list[str]] = []

    def command(argv: list[str]) -> int:
        received.append(argv)
        return 7

    monkeypatch.setattr(cli, "main", command)
    monkeypatch.setattr(
        startup,
        "resolve_postgres_dsn",
        lambda **_: pytest.fail("explicit offline commands must not resolve PostgreSQL"),
    )
    assert startup.main(["dependencies", "status", "--model-dir", "space in path"]) == 7
    assert received == [["dependencies", "status", "--model-dir", "space in path"]]


def test_default_startup_verifies_existing_connection_and_shows_sanitized_readiness(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    value = "postgresql://user:startup-password-canary@localhost/potbot"
    checked: list[str] = []
    monkeypatch.setattr(startup, "resolve_postgres_dsn", lambda **_: value)
    monkeypatch.setattr(startup, "validate_connection", checked.append)
    monkeypatch.delenv(credentials.PROFILE_ENV, raising=False)
    assert startup.main([]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out.splitlines()[0])
    assert payload["startup"] == {
        "status": "ready",
        "postgres_connection": "verified",
        "credential_profile": "default",
        "database_migrated": False,
        "model_downloaded": False,
    }
    assert checked == [value]
    assert "usage: potbot" in captured.out
    assert value not in captured.out + captured.err
    assert "startup-password-canary" not in captured.out + captured.err


def test_first_use_does_not_duplicate_successful_connection_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks: list[str] = []

    def resolved(**kwargs: object) -> str:
        validator = kwargs["validator"]
        assert callable(validator)
        validator("connection-checked-before-storage")
        return "connection-checked-before-storage"

    monkeypatch.setattr(startup, "resolve_postgres_dsn", resolved)
    monkeypatch.setattr(startup, "validate_connection", checks.append)
    monkeypatch.setattr(cli, "main", lambda _: 0)
    assert startup.main([]) == 0
    assert checks == ["connection-checked-before-storage"]


def test_failed_startup_does_not_claim_readiness(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unavailable(**_: object) -> str:
        raise SourceUnavailableError("Could not verify the PostgreSQL connection.")

    monkeypatch.setattr(startup, "resolve_postgres_dsn", unavailable)
    assert startup.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == "source_unavailable"


def test_help_needs_no_credential_store_or_postgres(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        startup,
        "resolve_postgres_dsn",
        lambda **_: pytest.fail("help must not access credentials"),
    )
    with pytest.raises(SystemExit) as completed:
        startup.main(["--help"])
    assert completed.value.code == 0
    assert "usage: potbot" in capsys.readouterr().out


def test_startup_and_credential_imports_do_not_load_optional_adapters(tmp_path: Path) -> None:
    program = """
import importlib.abc
import sys
blocked = {'keyring', 'psycopg', 'transformers', 'torch'}
class DenyAdapters(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in blocked:
            raise AssertionError('unexpected optional adapter: ' + fullname)
sys.meta_path.insert(0, DenyAdapters())
import potbot.credentials
import potbot.startup
assert not blocked.intersection(sys.modules)
print('isolated')
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
