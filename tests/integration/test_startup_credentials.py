"""Real PostgreSQL startup with an explicit test vault; not OS-vault qualification."""

from __future__ import annotations

import json
from urllib.parse import urlsplit, urlunsplit

import pytest

from potbot import credentials, startup

from .conftest import AppEnvironment

pytestmark = pytest.mark.postgres


class ContractVault:
    """Persist across independent store instances without modifying an OS account."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[service, username] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[service, username]


def test_first_run_connection_saved_second_startup_and_headless_override(
    app_env: AppEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = ContractVault()
    prompts: list[str] = []
    dsn = app_env.db.dsn
    monkeypatch.delenv("POTBOT_DSN", raising=False)
    monkeypatch.delenv("POTBOT_ADMIN_DSN", raising=False)
    monkeypatch.setenv(credentials.PROFILE_ENV, "startup-integration")
    monkeypatch.setattr(credentials, "native_backend", lambda: vault)
    monkeypatch.setattr(credentials, "_interactive_allowed", lambda *_: True)

    def entered(label: str) -> str:
        prompts.append(label)
        return dsn

    monkeypatch.setattr(credentials, "_hidden_prompt", entered)
    assert startup.main([]) == 0
    assert len(prompts) == 1
    assert vault.values == {
        (credentials.SERVICE_NAME, "startup-integration:postgres.application"): dsn,
    }
    first = capsys.readouterr()
    assert json.loads(first.out.splitlines()[0])["startup"]["status"] == "ready"
    # A fresh store is constructed on each startup. A cached value is usable
    # without an input terminal; another hidden prompt would be a regression.
    monkeypatch.setattr(credentials, "_interactive_allowed", lambda *_: False)
    assert startup.main([]) == 0
    assert len(prompts) == 1
    second = capsys.readouterr()
    # A missing value in a headless session fails before attempting input.
    monkeypatch.setenv(credentials.PROFILE_ENV, "missing-headless")
    assert startup.main([]) == 1
    missing = capsys.readouterr()
    assert json.loads(missing.err)["error"]["code"] == "invalid_input"
    assert len(prompts) == 1
    # The supported headless path validates the explicit URL but never saves it.
    monkeypatch.setenv("POTBOT_DSN", dsn)
    assert startup.main([]) == 0
    override = capsys.readouterr()
    assert len(vault.values) == 1
    combined = "".join(
        captured.out + captured.err for captured in (first, second, missing, override)
    )
    assert dsn not in combined
    assert urlsplit(dsn).password not in combined


def test_failed_real_connection_never_persists_url_or_password(
    app_env: AppEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = ContractVault()
    original = urlsplit(app_env.db.dsn)
    dsn = urlunsplit(original._replace(path="/potbot_test_startup_nonexistent_database"))
    monkeypatch.delenv("POTBOT_DSN", raising=False)
    monkeypatch.setenv(credentials.PROFILE_ENV, "startup-failure-integration")
    monkeypatch.setattr(credentials, "native_backend", lambda: vault)
    monkeypatch.setattr(credentials, "_interactive_allowed", lambda *_: True)
    monkeypatch.setattr(credentials, "_hidden_prompt", lambda _: dsn)
    assert startup.main([]) == 1
    assert vault.values == {}
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err.splitlines()[-1])
    assert error["error"]["code"] == "source_unavailable"
    assert dsn not in captured.err
    assert original.password not in captured.err
