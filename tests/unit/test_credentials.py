"""Credential boundaries: native selection, hidden prompts and nonleaking failures."""

from __future__ import annotations

import io
import json
import sys
import warnings
from types import SimpleNamespace
from typing import Any

import pytest

from potbot import credentials
from potbot.credentials import CredentialStore, resolve_postgres_dsn
from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    SourceUnavailableError,
)

SECRET = "postgresql://potbot:credential-canary-9f30@localhost:5432/potbot"


class FakeVault:
    """An explicitly injected test adapter, never an application fallback."""

    priority = 1

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.writes = 0

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.writes += 1
        self.values[service, username] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[service, username]


@pytest.mark.parametrize(
    ("platform_name", "module_name", "class_name"),
    [
        ("win32", "keyring.backends.Windows", "WinVaultKeyring"),
        ("darwin", "keyring.backends.macOS", "Keyring"),
        ("linux", "keyring.backends.SecretService", "Keyring"),
    ],
)
def test_only_explicit_builtin_native_backend_is_selected(
    monkeypatch: pytest.MonkeyPatch, platform_name: str, module_name: str, class_name: str
) -> None:
    imports: list[str] = []

    def imported(name: str) -> SimpleNamespace:
        imports.append(name)
        return SimpleNamespace(**{class_name: FakeVault})

    monkeypatch.setattr(credentials.importlib, "import_module", imported)
    assert isinstance(credentials.native_backend(platform_name), FakeVault)
    assert imports == [module_name]


@pytest.mark.parametrize("unavailable", ["missing", "disabled", "exception"])
def test_unavailable_native_backend_has_no_plugin_or_plaintext_fallback(
    monkeypatch: pytest.MonkeyPatch, unavailable: str
) -> None:
    calls: list[str] = []

    def imported(name: str) -> SimpleNamespace:
        calls.append(name)
        if unavailable == "missing":
            raise ImportError(SECRET)
        if unavailable == "exception":
            raise RuntimeError(SECRET)
        return SimpleNamespace(Keyring=lambda: SimpleNamespace(priority=0))

    monkeypatch.setattr(credentials.importlib, "import_module", imported)
    with pytest.raises(CapabilityUnavailableError) as error:
        credentials.native_backend("linux")
    assert SECRET not in str(error.value)
    assert calls == ["keyring.backends.SecretService"]


def test_unsupported_platform_fails_closed() -> None:
    with pytest.raises(CapabilityUnavailableError):
        credentials.native_backend("unsupported")


def test_profiles_and_application_admin_values_are_separate() -> None:
    vault = FakeVault()
    first = CredentialStore("one", backend=vault)
    second = CredentialStore("two", backend=vault)
    first.set("postgres.application", SECRET)
    first.set("postgres.admin", SECRET.replace("potbot:", "admin:"))
    assert first.get("postgres.application") == SECRET
    assert first.get("postgres.admin") != SECRET
    assert second.get("postgres.application") is None
    assert first.status() == {
        "profile": "one",
        "storage": "native_os_credential_vault",
        "application_configured": True,
        "admin_configured": True,
    }
    assert first.delete("postgres.application") is True
    assert first.delete("postgres.application") is False
    assert first.get("postgres.admin") is not None


@pytest.mark.parametrize("profile", ["", "a/b", "bad name", "a" * 65, "secret\nline"])
def test_invalid_profile_is_rejected(profile: str) -> None:
    with pytest.raises(InvalidInputError):
        CredentialStore(profile)


def test_arbitrary_credential_names_are_not_supported() -> None:
    with pytest.raises(InvalidInputError):
        CredentialStore(backend=FakeVault()).get("not-a-postgres-slot")


def test_environment_overrides_do_not_access_or_change_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> Any:
        pytest.fail("explicit environment must not access the credential store")

    monkeypatch.setattr(credentials, "native_backend", unavailable)
    environment = {"POTBOT_DSN": "host=localhost dbname=potbot password=secret"}
    assert resolve_postgres_dsn(environment=environment) == environment["POTBOT_DSN"]
    assert environment == {"POTBOT_DSN": "host=localhost dbname=potbot password=secret"}


def test_application_override_is_never_used_for_admin() -> None:
    with pytest.raises(InvalidInputError, match="POTBOT_ADMIN_DSN"):
        resolve_postgres_dsn(
            "admin",
            environment={"POTBOT_DSN": SECRET},
            store=CredentialStore(backend=FakeVault()),
            interactive=False,
        )


def test_first_use_validates_then_persists_and_next_process_reads_without_prompt() -> None:
    vault = FakeVault()
    output = io.StringIO()
    checks: list[str] = []

    def check(dsn: str) -> None:
        assert vault.writes == 0
        checks.append(dsn)

    assert (
        resolve_postgres_dsn(
            environment={},
            store=CredentialStore(backend=vault),
            interactive=True,
            prompt=lambda _: SECRET,
            validator=check,
            output=output,
        )
        == SECRET
    )
    assert checks == [SECRET]
    assert vault.writes == 1
    assert (
        resolve_postgres_dsn(
            environment={},
            store=CredentialStore(backend=vault),
            interactive=False,
            prompt=lambda _: pytest.fail("stored credential must bypass prompt"),
            validator=lambda _: pytest.fail("ordinary resolver does not probe a stored connection"),
        )
        == SECRET
    )
    assert SECRET not in output.getvalue()
    assert "credential-canary" not in output.getvalue()


def test_failed_first_connection_is_not_saved_or_printed() -> None:
    vault = FakeVault()
    output = io.StringIO()

    def check(dsn: str) -> None:
        raise RuntimeError(f"driver connection failure for {dsn}")

    with pytest.raises(SourceUnavailableError) as error:
        resolve_postgres_dsn(
            environment={},
            store=CredentialStore(backend=vault),
            interactive=True,
            prompt=lambda _: SECRET,
            validator=check,
            output=output,
        )
    assert vault.values == {}
    assert SECRET not in str(error.value) + output.getvalue()


def test_missing_headless_connection_never_prompts_or_saves() -> None:
    vault = FakeVault()
    with pytest.raises(InvalidInputError, match="POTBOT_DSN"):
        resolve_postgres_dsn(
            environment={},
            store=CredentialStore(backend=vault),
            interactive=False,
            prompt=lambda _: pytest.fail("headless invocation must not prompt"),
        )
    assert vault.writes == 0


def test_unavailable_vault_is_detected_before_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable() -> Any:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(credentials, "native_backend", unavailable)
    with pytest.raises(CapabilityUnavailableError) as error:
        resolve_postgres_dsn(
            environment={},
            interactive=True,
            prompt=lambda _: pytest.fail("unavailable vault must not collect a secret"),
        )
    assert SECRET not in str(error.value)
    with pytest.raises(InvalidInputError, match="POTBOT_DSN"):
        resolve_postgres_dsn(environment={}, interactive=False)


def test_success_is_not_claimed_when_vault_drops_or_mangles_write() -> None:
    class BrokenVault(FakeVault):
        def set_password(self, service: str, username: str, password: str) -> None:
            self.values[service, username] = "not-the-requested-value"

    with pytest.raises(CapabilityUnavailableError, match="not confirmed") as error:
        CredentialStore(backend=BrokenVault()).set("postgres.application", SECRET)
    assert SECRET not in str(error.value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://localhost/potbot",
        "postgresql://localhost",
        "postgresql:///potbot",
        "postgresql://localhost/db/extra",
        "postgresql://localhost:65536/potbot",
        "postgresql://localhost:bad/potbot",
        "postgresql://localhost/potbot#fragment",
        "postgresql://first,second/potbot",
        "postgresql://first%2csecond/potbot",
        "postgresql://localhost/potbot?host=first,second",
        "postgresql://localhost/potbot?host=first%2Csecond",
        "postgresql://localhost/potbot?port=5432,5433",
        "postgresql://localhost/potbot?sslmode=require&sslmode=disable",
        "postgresql://localhost/potbot\n",
        "x" * 4097,
    ],
)
def test_new_stored_urls_require_one_explicit_endpoint_and_database(value: str) -> None:
    with pytest.raises(InvalidInputError) as error:
        credentials.validate_postgres_url(value)
    assert value not in str(error.value) if value else True


@pytest.mark.parametrize(
    "value", [SECRET, "postgres://localhost/potbot?sslmode=require", "postgresql://[::1]:5432/db"]
)
def test_valid_postgres_urls_are_preserved(value: str) -> None:
    assert credentials.validate_postgres_url(value) == value


def test_hidden_prompt_refuses_getpass_echo_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def echo_fallback(_: str) -> str:
        warnings.warn("cannot disable echo", credentials.getpass.GetPassWarning, stacklevel=1)
        pytest.fail("getpass must stop before reading echoed input")

    monkeypatch.setattr(credentials.getpass, "getpass", echo_fallback)
    with pytest.raises(InvalidInputError, match="hidden credential prompt"):
        credentials._hidden_prompt("PostgreSQL URL: ")


def test_connection_check_is_read_only_bounded_and_contains_no_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []
    options: dict[str, Any] = {}

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *unused: object) -> None:
            pass

        def execute(self, statement: str) -> SimpleNamespace:
            statements.append(statement)
            return SimpleNamespace(fetchone=lambda: (1,))

    def connect(dsn: str, **kwargs: Any) -> Connection:
        assert dsn == SECRET
        options.update(kwargs)
        return Connection()

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    credentials.validate_connection(SECRET)
    assert statements == ["SELECT 1"]
    assert options["connect_timeout"] == 5
    assert "default_transaction_read_only=on" in options["options"]
    assert "statement_timeout=3000" in options["options"]
    assert "lock_timeout=3000" in options["options"]


def test_connection_error_never_exposes_raw_driver_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def connect(*args: object, **kwargs: object) -> Any:
        raise RuntimeError(SECRET)

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    with pytest.raises(SourceUnavailableError) as error:
        credentials.validate_connection(SECRET)
    assert SECRET not in str(error.value)


def test_status_set_delete_management_output_has_no_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = FakeVault()
    monkeypatch.setattr(credentials, "native_backend", lambda: vault)
    monkeypatch.setattr(credentials, "_interactive_allowed", lambda *_: True)
    monkeypatch.setattr(credentials, "_hidden_prompt", lambda _: SECRET)
    monkeypatch.setattr(credentials, "validate_connection", lambda _: None)
    assert credentials.main(["--profile", "management-test", "set"]) == 0
    assert credentials.main(["--profile", "management-test", "status"]) == 0
    assert credentials.main(["--profile", "management-test", "delete"]) == 0
    result = capsys.readouterr()
    payloads = [json.loads(line) for line in result.out.splitlines()]
    assert payloads[0]["data"]["saved"] is True
    assert payloads[1]["data"]["application_configured"] is True
    assert payloads[2]["data"]["deleted"] is True
    assert SECRET not in result.out + result.err
    assert "credential-canary" not in result.out + result.err
    assert vault.values == {}


def test_empty_explicit_profile_does_not_silently_select_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert credentials.main(["--profile", "", "status"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_input"


@pytest.mark.parametrize(
    ("message", "sqlstate", "reason"),
    [
        (
            "password authentication failed for private-user",
            "28P01",
            "authentication_or_access_rejected",
        ),
        ("missing private-database", "3D000", "database_not_found"),
        ("could not translate host name private-host", None, "host_resolution_failed"),
        ("connection refused at private-host", None, "connection_refused"),
        ("connection timed out at private-host", None, "connection_timed_out"),
        ("SSL error: certificate verify failed for private-host", None, "tls_connection_failed"),
        ("no pg_hba.conf entry for private-user", None, "authentication_or_access_rejected"),
        ("opaque private diagnostic", None, "unclassified_connection_failure"),
    ],
)
def test_connection_failure_reports_fixed_reason_without_private_driver_text(
    monkeypatch: pytest.MonkeyPatch, message: str, sqlstate: str | None, reason: str
) -> None:
    class DriverFailure(Exception):
        pass

    failure = DriverFailure(message + " " + SECRET)
    failure.sqlstate = sqlstate  # type: ignore[attr-defined]

    def connect(*args: object, **kwargs: object) -> Any:
        raise failure

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    with pytest.raises(SourceUnavailableError) as error:
        credentials.validate_connection(SECRET)
    rendered = str(error.value)
    assert f"Diagnostic: {reason}." in rendered
    assert SECRET not in rendered
    assert "private-" not in rendered
    assert error.value.__suppress_context__ is True
