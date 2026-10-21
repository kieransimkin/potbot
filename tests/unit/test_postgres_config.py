"""Pure configuration and manifest checks; no driver or service is required."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.persistence import harness
from potbot.persistence.harness import (
    DSN_ENV,
    HarnessError,
    RunHandle,
    TestDatabaseConfig,
    main,
    read_manifest,
    write_manifest,
)

TEST_DSN = "postgresql://potbot_test:fixture-secret@127.0.0.1:55432/potbot_test"


def sample_handle() -> RunHandle:
    run_id = uuid4()
    return RunHandle(uuid4(), run_id, f"potbot_test_{run_id.hex}", "a" * 64, datetime.now(UTC))


def test_explicit_dsn_is_required_even_if_a_production_default_exists() -> None:
    with pytest.raises(HarnessError, match=DSN_ENV) as failure:
        TestDatabaseConfig.from_env({"DATABASE_URL": TEST_DSN})
    assert failure.value.code == "missing_test_dsn"


def test_dedicated_dsn_is_parsed_without_exposing_password() -> None:
    config = TestDatabaseConfig.from_env({DSN_ENV: TEST_DSN})
    assert config.host == "127.0.0.1"
    assert config.port == 55432
    assert config.database == "potbot_test"
    assert "fixture-secret" not in repr(config)
    assert TEST_DSN not in repr(config)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://potbot_test:fixture-secret@db.example.com:55432/potbot_test",
        "postgresql://potbot_test:fixture-secret@127.0.0.1:55432/production",
        "postgresql://admin:fixture-secret@127.0.0.1:55432/potbot_test",
        "postgresql://potbot_test@127.0.0.1:55432/potbot_test",
        "postgresql://potbot_test:fixture-secret@127.0.0.1/potbot_test",
        "postgresql://potbot_test:fixture-secret@127.0.0.1:bad/potbot_test",
        "postgresql://potbot_test:fixture-secret@127.0.0.1:55432/potbot_test?host=remote",
        "postgresql://potbot_test:fixture-secret@127.0.0.1:55432/potbot_test#fragment",
        "postgresql://potbot_test:fixture-secret@127.0.0.1:55432/potbot_test_prod%2Fdata",
        "postgresql://potbot_test:fixture-secret@127.0.0.1:55432/potbot_test_../data",
        "host=127.0.0.1 dbname=potbot_test password=fixture-secret",
        "",
    ],
)
def test_unsafe_or_implicit_connection_targets_are_rejected(dsn: str) -> None:
    with pytest.raises(HarnessError) as failure:
        TestDatabaseConfig(dsn)
    assert failure.value.code == "unsafe_target"
    assert "fixture-secret" not in str(failure.value)


def test_ipv6_loopback_and_unique_fixture_database_are_supported() -> None:
    config = TestDatabaseConfig(
        "postgresql://potbot_test:fixture-secret@[::1]:55432/potbot_test_abc123"
    )
    assert config.host == "::1"
    assert config.database == "potbot_test_abc123"


@pytest.mark.parametrize(("host", "expected"), [("localhost", "127.0.0.1"), ("[::1]", "::1")])
def test_driver_pins_loopback_despite_ambient_libpq_settings(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    expected: str,
) -> None:
    class StoppedConnection(Exception):
        pass

    observed: dict[str, object] = {}

    def capture_connection(dsn: str, **options: object) -> None:
        observed.update(options)
        raise StoppedConnection

    driver = SimpleNamespace(connect=capture_connection, Error=StoppedConnection)
    monkeypatch.setattr(harness, "_driver", lambda: driver)
    monkeypatch.setenv("PGHOSTADDR", "203.0.113.9")
    monkeypatch.setenv("PGOPTIONS", "-c search_path=public")
    config = TestDatabaseConfig(f"postgresql://potbot_test:fixture-secret@{host}:55432/potbot_test")
    with pytest.raises(HarnessError) as failure, harness._connection(config):
        pytest.fail("The stub should prevent any actual connection.")
    assert failure.value.code == "database_unavailable"
    assert observed["hostaddr"] == expected
    assert "search_path=pg_catalog" in str(observed["options"])


def test_manifest_round_trip_has_no_database_credentials(tmp_path: Path) -> None:
    handle = sample_handle()
    path = tmp_path / "run.json"
    write_manifest(path, handle)
    assert read_manifest(path) == handle
    assert "dsn" not in json.loads(path.read_text())
    assert "a" * 64 not in repr(handle)
    assert path.stat().st_mode & 0o077 == 0


def test_manifest_write_refuses_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    original = sample_handle()
    write_manifest(path, original)
    with pytest.raises(HarnessError) as failure:
        write_manifest(path, sample_handle())
    assert failure.value.code == "manifest_write_failed"
    assert read_manifest(path) == original


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format_version", 2),
        ("format_version", True),
        ("schema_name", "public"),
        ("ownership_token", "wrong"),
        ("created_at", "2026-01-01T00:00:00"),
        ("run_id", "invalid"),
        ("database_id", "invalid"),
    ],
)
def test_manifest_rejects_invalid_scope_or_identity(field: str, value: object) -> None:
    manifest = sample_handle().to_dict()
    manifest[field] = value
    with pytest.raises(HarnessError) as failure:
        RunHandle.from_dict(manifest)
    assert failure.value.code == "invalid_manifest"


def test_handle_cannot_name_an_arbitrary_schema() -> None:
    with pytest.raises(HarnessError):
        replace(sample_handle(), schema_name="public")


def test_cli_reports_missing_configuration_as_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(DSN_ENV, raising=False)
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    assert main(["bootstrap"]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    failure = json.loads(captured.err)
    assert failure["ok"] is False
    assert failure["error"] == "missing_test_dsn"
    assert "fixture-secret" not in captured.err
