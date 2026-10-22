"""Public CLI, native protocol boundary, and fail-closed host readiness tests."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.security import broker, cli, public

_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures/security/native-public-signature-bundle.json"
)
_PIN = "28996c77aba50985b85b6c81f0e60d5b405b81a45efdafb3fda5d9f8581720e8"
_NOW = 1792584000
_WORKSPACE = "00000000-0000-4000-8000-000000000001"
_REQUEST = "00000000-0000-4000-8000-000000000002"


def _bundle() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_bytes())  # type: ignore[no-any-return]


def _request() -> dict[str, object]:
    return {
        "workspace_id": _WORKSPACE,
        "audience": "test",
        "domain": "potbot.event.v2",
        "payload_type_ref": "syn://type/ember",
        "payload_sha256": hashlib.sha256(b"fixture").hexdigest(),
        "request_id": _REQUEST,
        "validity_seconds": 300,
    }


def _policy() -> dict[str, object]:
    return {
        "workspace_id": _WORKSPACE,
        "allowed_audiences": ["test"],
        "allowed_domains": ["potbot.event.v2"],
        "allowed_payload_types": ["syn://type/ember"],
    }


def _preflight() -> dict[str, object]:
    return {
        "schema_version": 1,
        "protection_profile": broker.PROTECTION_PROFILE,
        "platform": "linux"
        if sys.platform == "linux"
        else "windows"
        if os.name == "nt"
        else "unsupported",
        "ready": False,
        "required_locked_memory_bytes": broker.REQUIRED_LOCKED_MEMORY_BYTES,
        "locked_memory_limit_bytes": 8 * 1024 * 1024,
        "reason": "locked-memory-limit",
    }


def _scope() -> list[str]:
    return [
        "--expected-signer-hash",
        _PIN,
        "--workspace-id",
        _WORKSPACE,
        "--audience",
        "test",
        "--domain",
        "potbot.event.v2",
        "--payload-type-ref",
        "syn://type/ember",
    ]


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(public.time, "time", lambda: _NOW)


@pytest.fixture
def public_files(tmp_path: Path) -> tuple[Path, Path]:
    bundle = tmp_path / "bundle.json"
    bundle.write_bytes(_FIXTURE.read_bytes())
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"fixture")
    return bundle, payload


def test_offline_verify_never_opens_native_broker(
    public_files: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("offline verification must not open custody")

    monkeypatch.setattr(cli, "NativeIdentityBroker", forbidden)
    bundle, payload = public_files
    assert cli.main(["verify", "--bundle", str(bundle), "--payload", str(payload), *_scope()]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["verified"] is True
    assert result["signer_hash"] == _PIN
    assert result["replay_protected"] is False


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("--expected-signer-hash", "0" * 64),
        ("--audience", "another"),
        ("--workspace-id", "00000000-0000-4000-8000-000000000099"),
        ("--domain", "potbot.artifact.v2"),
        ("--payload-type-ref", "syn://type/document"),
    ],
)
def test_cli_enforces_independent_scope(
    public_files: tuple[Path, Path],
    field: str,
    replacement: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle, payload = public_files
    scope = _scope()
    scope[scope.index(field) + 1] = replacement
    assert cli.main(["verify", "--bundle", str(bundle), "--payload", str(payload), *scope]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert json.loads(captured.err)["error"] == "invalid_input"


def test_cli_rejects_payload_changes_and_verifies_published_key(
    public_files: tuple[Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, payload = public_files
    payload.write_bytes(b"changed")
    assert cli.main(["verify", "--bundle", str(bundle), "--payload", str(payload), *_scope()]) == 2
    assert "signed digest" in capsys.readouterr().err
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps(_bundle()["identity"]))
    assert (
        cli.main(["verify-public", "--identity", str(identity), "--expected-signer-hash", _PIN])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["verified"] is True


def test_accept_durably_consumes_request_once(
    public_files: tuple[Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, payload = public_files
    ledger = tmp_path / "accepted.sqlite"
    args = [
        "accept",
        "--bundle",
        str(bundle),
        "--payload",
        str(payload),
        "--replay-db",
        str(ledger),
        *_scope(),
    ]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["replay_protected"] is True
    assert cli.main(args) == 2
    assert "already consumed" in capsys.readouterr().err


def test_output_collision_is_rejected_before_replay_reservation(
    public_files: tuple[Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, payload = public_files
    ledger = tmp_path / "accepted.sqlite"
    original = payload.read_bytes()
    assert (
        cli.main(
            [
                "accept",
                "--bundle",
                str(bundle),
                "--payload",
                str(payload),
                "--replay-db",
                str(ledger),
                "--output",
                str(payload),
                *_scope(),
            ]
        )
        == 2
    )
    assert "must not replace" in capsys.readouterr().err
    assert payload.read_bytes() == original
    assert not ledger.exists()


@pytest.mark.parametrize("flag", ["--password", "--password-fd", "--mnemonic"])
def test_private_input_options_are_rejected_without_echoing_values(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["preflight", flag, "sensitive-marker-must-not-be-echoed"])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "sensitive-marker" not in captured.err + captured.out
    assert "private inputs belong only on the native terminal" in captured.err


def test_help_does_not_import_optional_crypto_database_or_model_backends() -> None:
    script = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split(".")[0] in {"cryptography", "sqlite3", "psycopg", "torch", "transformers"}:
        raise AssertionError("unexpected optional dependency: " + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from potbot.cli import main
raise SystemExit(main(["identity", "--help"]))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert "verify-public" in completed.stdout


@pytest.mark.parametrize("operation", ["sign", "init", "public", "backup"])
def test_input_output_collisions_stop_before_custody(
    operation: str,
    public_files: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("collision must be detected before invoking the broker")

    monkeypatch.setattr(cli, "NativeIdentityBroker", forbidden)
    _, payload = public_files
    vault = tmp_path / "private" / "vault.json"
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(_policy()))
    extra: list[str] = []
    output = vault
    if operation == "sign":
        extra = ["--payload", str(payload), *_scope()]
        output = payload
    elif operation == "init":
        extra = ["--policy", str(policy)]
        output = policy
    assert cli.main([operation, "--vault", str(vault), "--output", str(output), *extra]) == 2
    assert "must not replace" in capsys.readouterr().err
    assert not vault.parent.exists()


def test_public_output_hardlink_collision_is_rejected(
    public_files: tuple[Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, payload = public_files
    alias = tmp_path / "payload-alias"
    os.link(payload, alias)
    assert (
        cli.main(
            [
                "verify",
                "--bundle",
                str(bundle),
                "--payload",
                str(payload),
                "--output",
                str(alias),
                *_scope(),
            ]
        )
        == 2
    )
    assert "must not replace" in capsys.readouterr().err
    assert payload.read_bytes() == b"fixture"


def test_payload_digest_rejects_nonregular_files() -> None:
    with pytest.raises(InvalidInputError, match="regular file"):
        cli._payload_digest(Path(os.devnull))


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO behavior")
def test_payload_fifo_fails_without_waiting_for_a_writer(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(InvalidInputError, match="regular file"):
        cli._payload_digest(fifo)


def test_payload_change_during_hash_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "payload"
    payload.write_bytes(b"original")
    original = hashlib.file_digest

    def mutate(stream: Any, algorithm: str) -> Any:
        result = original(stream, algorithm)
        payload.write_bytes(b"changed and longer")
        return result

    monkeypatch.setattr(hashlib, "file_digest", mutate)
    with pytest.raises(InvalidInputError, match="changed"):
        cli._payload_digest(payload)


def test_native_sign_request_contains_only_fixed_public_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(_bundle()).encode())

    monkeypatch.setattr(broker, "_executable", lambda _: Path("/trusted/potbot-identity"))
    monkeypatch.setattr(broker.subprocess, "run", run)
    monkeypatch.setattr(broker.NativeIdentityBroker, "_require_ready", lambda _: None)
    vault = tmp_path / "vault"
    result = broker.NativeIdentityBroker().sign(vault, _request(), expected_signer_hash=_PIN)
    assert result == _bundle()
    args, options = calls.pop()
    assert args[1:] == ["sign", "--vault", str(vault), "--expect-signer", _PIN]
    checked = json.loads(options["input"])
    assert checked == {**_request(), "payload_type_ref": "synny://type/ember"}
    assert options["stderr"] is None
    assert "env" not in options and "pass_fds" not in options
    assert all("password" not in arg for arg in args)


@pytest.mark.parametrize(
    "field", ["password", "private_key", "signing_epoch", "issued_at", "derivation_path"]
)
def test_native_request_cannot_supply_private_or_broker_chosen_fields(
    field: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(_: object) -> None:
        raise AssertionError("bad requests must fail before custody")

    monkeypatch.setattr(broker.NativeIdentityBroker, "_require_ready", forbidden)
    with pytest.raises(public.PublicVerificationError, match="exactly"):
        broker.NativeIdentityBroker().sign(
            tmp_path / "vault", {**_request(), field: "forbidden"}, expected_signer_hash=_PIN
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("request_id", "00000000-0000-4000-8000-000000000099"), ("validity_seconds", 299)],
)
def test_native_valid_signature_must_match_the_exact_authorized_request(
    field: str, value: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(broker.NativeIdentityBroker, "_require_ready", lambda _: None)
    monkeypatch.setattr(broker.NativeIdentityBroker, "_run", lambda *args, **kwargs: (0, _bundle()))
    with pytest.raises(broker.NativeBrokerError, match="exact authorized request"):
        broker.NativeIdentityBroker().sign(
            tmp_path / "vault", {**_request(), field: value}, expected_signer_hash=_PIN
        )


def test_native_public_result_is_exact_descriptor_without_a_new_trust_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        broker.NativeIdentityBroker, "_run", lambda *args, **kwargs: (0, _bundle()["identity"])
    )
    native = broker.NativeIdentityBroker()
    assert native.public(tmp_path / "vault", expected_signer_hash=_PIN) == _bundle()["identity"]
    assert "verified" not in native.public(tmp_path / "vault")
    with pytest.raises(public.PublicVerificationError, match="pinned"):
        native.public(tmp_path / "vault", expected_signer_hash="0" * 64)


def test_native_failure_never_exposes_unvalidated_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(broker, "_executable", lambda _: Path("/trusted/potbot-identity"))
    monkeypatch.setattr(
        broker.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 1, stdout=b"sensitive-unexpected-output"
        ),
    )
    with pytest.raises(broker.NativeBrokerError) as error:
        broker.NativeIdentityBroker().public(tmp_path / "vault")
    assert "sensitive-unexpected-output" not in str(error.value)


def test_create_validates_policy_and_leaves_private_directory_creation_to_native(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(_policy()))
    vault = tmp_path / "new-private-parent" / "vault.json"
    calls: list[list[str]] = []

    def invoke(self: object, arguments: list[str], **kwargs: object) -> tuple[int, dict[str, Any]]:
        calls.append(arguments)
        snapshot = Path(arguments[arguments.index("--policy") + 1])
        assert snapshot != policy
        assert json.loads(snapshot.read_text()) == {
            **_policy(),
            "allowed_payload_types": ["synny://type/ember"],
        }
        assert not vault.parent.exists()
        return 0, _bundle()["identity"]

    monkeypatch.setattr(broker.NativeIdentityBroker, "_require_ready", lambda _: None)
    monkeypatch.setattr(broker.NativeIdentityBroker, "_run", invoke)
    assert broker.NativeIdentityBroker().initialize(vault, policy) == _bundle()["identity"]
    assert calls[0][0] == "init"
    assert not Path(calls[0][calls[0].index("--policy") + 1]).exists()
    assert not vault.parent.exists()
    assert json.loads(policy.read_text()) == _policy()


@pytest.mark.parametrize(
    "change",
    [
        {"allowed_payload_types": ["syn://type/ember", "synny://type/ember"]},
        {"allowed_payload_types": ["synny://type/unregistered-example"]},
        {"allowed_payload_types": []},
        {"allowed_audiences": "test"},
        {"allowed_domains": ["potbot.hourly-delegation.v1"]},
        {"workspace_id": True},
        {"private_key": "never-allowed"},
    ],
)
def test_signing_policy_rejects_ambiguous_unknown_or_private_fields(
    change: dict[str, object],
) -> None:
    with pytest.raises(InvalidInputError):
        broker.validate_signing_policy({**_policy(), **change})


def test_preflight_blocks_secret_operations_before_opening_the_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def invoke(
        self: object, arguments: list[str], **kwargs: object
    ) -> tuple[int, dict[str, object]]:
        calls.append(arguments)
        return 78, _preflight()

    monkeypatch.setattr(broker.NativeIdentityBroker, "_run", invoke)
    native = broker.NativeIdentityBroker()
    report = native.preflight()
    assert report["ready"] is False
    assert report["argon2_memory_bytes"] == 64 * 1024 * 1024
    assert report["required_locked_memory_bytes"] == 96 * 1024 * 1024
    assert report["unprotected_fallback"] is False
    with pytest.raises(broker.NativeBrokerError, match="no fallback"):
        native.sign(tmp_path / "private" / "vault", _request(), expected_signer_hash=_PIN)
    assert calls == [["preflight"], ["preflight"]]
    assert not (tmp_path / "private").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"ready": "false"},
        {"required_locked_memory_bytes": 64 * 1024 * 1024},
        {"locked_memory_limit_bytes": -1},
        {"reason": "unsafe-fallback"},
        {"private_key": "not-public"},
        {"ready": True, "reason": "ready"},
    ],
)
def test_preflight_rejects_weakened_or_inconsistent_native_contract(
    change: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        broker.NativeIdentityBroker,
        "_run",
        lambda *args, **kwargs: (78, {**_preflight(), **change}),
    )
    with pytest.raises(broker.NativeBrokerError):
        broker.NativeIdentityBroker().preflight()


def test_native_build_is_locked_release_and_hashes_selected_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "src/main.rs"):
        (source / name).write_text("test fixture " + name)
    target = tmp_path / "build"
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        binary = target / "release" / broker._BINARY_NAME
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"synthetic executable for build receipt test")
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["cwd"] == source
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(broker.shutil, "which", lambda _: "/trusted/cargo")
    monkeypatch.setattr(broker.subprocess, "run", run)
    result = broker.build_native_broker(source=source, target_directory=target)
    assert calls == [
        [
            "/trusted/cargo",
            "+1.90.0",
            "build",
            "--locked",
            "--release",
            "--manifest-path",
            str(source / "Cargo.toml"),
            "--target-dir",
            str(target),
        ]
    ]
    assert result["source_sha256"] == broker._source_digest(source)
    assert result["cargo_locked"] is True
    assert (
        result["executable_sha256"]
        == hashlib.sha256((target / "release" / broker._BINARY_NAME).read_bytes()).hexdigest()
    )


def test_unbuilt_broker_is_not_discovered_from_current_directory_or_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unrelated = tmp_path / broker._BINARY_NAME
    unrelated.write_text("unrelated executable")
    unrelated.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(broker, "native_source_directory", lambda *args: tmp_path / "source")
    monkeypatch.setattr(
        broker, "native_build_target", lambda *args: tmp_path / "missing-pinned-build"
    )
    with pytest.raises(broker.NativeBrokerError, match="not built"):
        broker._executable(None)


def test_public_input_and_policy_reject_nonregular_files(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError, match="regular file"):
        cli._bounded_read(Path(os.devnull), public.MAX_PUBLIC_DOCUMENT_BYTES)
    with pytest.raises(public.PublicVerificationError, match="regular file"):
        broker.NativeIdentityBroker().initialize(tmp_path / "vault", Path(os.devnull))


def test_build_receipt_cannot_overwrite_its_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    executable = tmp_path / "potbot-identity"
    executable.write_bytes(b"preserved native executable")
    monkeypatch.setattr(
        cli, "build_native_broker", lambda **kwargs: {"executable": str(executable)}
    )
    assert cli.main(["build", "--output", str(executable)]) == 2
    assert "must not replace" in capsys.readouterr().err
    assert executable.read_bytes() == b"preserved native executable"
