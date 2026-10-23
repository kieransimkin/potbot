"""Public-only Python adapter for the fixed native identity broker protocol.

Passwords and recovery phrases are handled directly by the native process on
its controlling terminal. Python never opens a vault, handles private material,
or offers a weaker memory-protection mode.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from potbot.domain.errors import CapabilityUnavailableError, DomainError
from potbot.domain.syn import type_identity
from potbot.semantics.registry import strict_json

from .public import (
    MAX_PUBLIC_DOCUMENT_BYTES,
    SIGNATURE_DOMAINS,
    PublicVerificationError,
    VerificationPolicy,
    load_public_document,
    verify_public_identity,
    verify_signature_bundle_digest,
)

RUST_TOOLCHAIN = "1.90.0"
PROTECTION_PROFILE = "potbot-native-password-v1"
KDF_MEMORY_BYTES = 64 * 1024 * 1024
REQUIRED_LOCKED_MEMORY_BYTES = 96 * 1024 * 1024
_BINARY_NAME = "potbot-identity.exe" if os.name == "nt" else "potbot-identity"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_AUDIENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}\Z")
_REQUEST_FIELDS = frozenset(
    (
        "workspace_id",
        "audience",
        "domain",
        "payload_type_ref",
        "payload_sha256",
        "request_id",
        "validity_seconds",
    )
)
_POLICY_FIELDS = frozenset(
    ("workspace_id", "allowed_audiences", "allowed_payload_types", "allowed_domains")
)


class NativeBrokerError(CapabilityUnavailableError):
    """The native custody boundary could not complete its fixed public protocol."""


def native_source_directory(source: Path | None = None) -> Path:
    """Locate checked-in or wheel-packaged Rust source without searching the cwd."""
    candidates = (
        (source,)
        if source is not None
        else (
            Path(__file__).resolve().parents[3] / "native" / "potbot-identity",
            Path(__file__).resolve().parent / "native_source",
        )
    )
    for candidate in candidates:
        path = candidate.resolve()
        if (
            all(
                (path / name).is_file()
                for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml")
            )
            and (path / "src" / "main.rs").is_file()
        ):
            return path
    raise NativeBrokerError("pinned native identity source is unavailable in this installation")


def _source_digest(source: Path) -> str:
    hasher = hashlib.sha256()
    paths = [source / name for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml")]
    paths.extend(sorted((source / "src").rglob("*.rs")))
    for path in paths:
        relative = path.relative_to(source).as_posix().encode("utf-8")
        data = path.read_bytes()
        hasher.update(len(relative).to_bytes(8, "big"))
        hasher.update(relative)
        hasher.update(len(data).to_bytes(8, "big"))
        hasher.update(data)
    return hasher.hexdigest()


def _cache_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return base / "potbot" / "identity"


def native_build_target(source: Path | None = None) -> Path:
    selected = native_source_directory(source)
    return _cache_root() / _source_digest(selected) / "target"


def build_native_broker(
    *, source: Path | None = None, target_directory: Path | None = None
) -> dict[str, object]:
    """Build the pinned Rust toolchain/dependency lockfile in release mode."""
    selected = native_source_directory(source)
    before = _source_digest(selected)
    target = target_directory.resolve() if target_directory else native_build_target(selected)
    cargo = shutil.which("cargo")
    if cargo is None:
        raise NativeBrokerError("Rust/rustup cargo is required to build the native identity broker")
    try:
        completed = subprocess.run(
            [
                cargo,
                f"+{RUST_TOOLCHAIN}",
                "build",
                "--locked",
                "--release",
                "--manifest-path",
                str(selected / "Cargo.toml"),
                "--target-dir",
                str(target),
            ],
            cwd=selected,
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            stderr=sys.stderr,
            check=False,
            timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NativeBrokerError("native identity build could not complete") from error
    if completed.returncode:
        raise NativeBrokerError(
            "native identity build failed; the pinned Rust toolchain is required"
        )
    if _source_digest(selected) != before:
        raise NativeBrokerError(
            "native source changed during the build; rebuild the current source"
        )
    executable = target / "release" / _BINARY_NAME
    if not executable.is_file():
        raise NativeBrokerError("native identity build did not produce the expected executable")
    return {
        "schema_version": 1,
        "executable": str(executable),
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "source_sha256": before,
        "rust_toolchain": RUST_TOOLCHAIN,
        "cargo_locked": True,
        "profile": "release",
    }


def _executable(explicit: Path | None) -> Path:
    if explicit is not None:
        candidates = [explicit.resolve()]
    else:
        source = native_source_directory()
        candidates = [
            native_build_target(source) / "release" / _BINARY_NAME,
        ]
    for candidate in candidates:
        if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return candidate
    raise NativeBrokerError("native identity broker is not built; run 'potbot identity build'")


def _native_object(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_PUBLIC_DOCUMENT_BYTES:
        raise NativeBrokerError("native broker returned an oversized public response")
    try:
        value = strict_json(raw)
    except (DomainError, ValueError, TypeError) as error:
        raise NativeBrokerError("native broker returned invalid public JSON") from error
    if not isinstance(value, dict):
        raise NativeBrokerError("native broker returned a non-object public response")
    return value


def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise PublicVerificationError(f"invalid {label}")
    return value


def _descriptor(value: Mapping[str, object], expected: str | None) -> dict[str, Any]:
    descriptor = load_public_document(value)
    # Local creation/export establishes only structural key/hash consistency when
    # no external pin is supplied. Do not label that operation identity verification.
    selected = (
        expected
        if expected is not None
        else _hash(descriptor.get("signer_hash"), "published fingerprint")
    )
    verify_public_identity(descriptor, expected_signer_hash=selected)
    return descriptor


def validate_signing_policy(
    value: Mapping[str, object], *, known_type_refs: frozenset[str] | None = None
) -> dict[str, object]:
    """Validate and snapshot public native signing policy against a trusted catalog."""
    if set(value) != _POLICY_FIELDS:
        raise PublicVerificationError("signing policy requires exactly its four scope fields")
    lists: dict[str, list[str]] = {}
    for field, maximum in (
        ("allowed_audiences", 128),
        ("allowed_payload_types", 512),
        ("allowed_domains", 3),
    ):
        supplied = value[field]
        if (
            not isinstance(supplied, list)
            or not 1 <= len(supplied) <= maximum
            or any(not isinstance(item, str) for item in supplied)
        ):
            raise PublicVerificationError("invalid bounded signing policy allowlist")
        selected = list(supplied)
        if field == "allowed_payload_types":
            selected = ["synny://type/" + type_identity(item) for item in selected]
        if len(set(selected)) != len(selected):
            raise PublicVerificationError("duplicate signing policy identity")
        lists[field] = sorted(selected)
    if any(_AUDIENCE.fullmatch(item) is None for item in lists["allowed_audiences"]):
        raise PublicVerificationError("invalid allowed audience")
    if any(item not in SIGNATURE_DOMAINS for item in lists["allowed_domains"]):
        raise PublicVerificationError("invalid allowed signature domain")
    workspace = value["workspace_id"]
    if not isinstance(workspace, str):
        raise PublicVerificationError("invalid policy workspace UUID")
    VerificationPolicy(
        expected_signer_hash="0" * 64,
        audience=lists["allowed_audiences"][0],
        workspace_id=workspace,
        domain=lists["allowed_domains"][0],
        allowed_payload_type_refs=frozenset(lists["allowed_payload_types"]),
        known_type_refs=known_type_refs,
    )
    return {"workspace_id": workspace, **lists}


def _request_policy(
    request: Mapping[str, object], expected_signer_hash: str, known_type_refs: frozenset[str] | None
) -> tuple[dict[str, object], VerificationPolicy]:
    if set(request) != _REQUEST_FIELDS:
        raise PublicVerificationError(
            "signing request requires exactly its versioned public fields"
        )
    value = load_public_document(request)
    _hash(value["payload_sha256"], "payload digest")
    if not isinstance(value["request_id"], str) or _UUID.fullmatch(value["request_id"]) is None:
        raise PublicVerificationError("invalid signing request UUID")
    validity = value["validity_seconds"]
    if not isinstance(validity, int) or isinstance(validity, bool) or not 1 <= validity <= 300:
        raise PublicVerificationError("signing validity must be between 1 and 300 seconds")
    value["payload_type_ref"] = "synny://type/" + type_identity(value["payload_type_ref"])
    policy = VerificationPolicy(
        expected_signer_hash=expected_signer_hash,
        audience=value["audience"],
        workspace_id=value["workspace_id"],
        domain=value["domain"],
        allowed_payload_type_refs=frozenset((value["payload_type_ref"],)),
        known_type_refs=known_type_refs,
    )
    return value, policy


class NativeIdentityBroker:
    """Run only fixed native commands and validate every public result before returning it."""

    def __init__(self, executable: Path | None = None) -> None:
        self.executable = executable

    def _run(
        self, arguments: list[str], *, public_input: bytes | None = None, ready_probe: bool = False
    ) -> tuple[int, dict[str, Any]]:
        executable = _executable(self.executable)
        try:
            completed = subprocess.run(
                [str(executable), *arguments],
                input=public_input,
                stdout=subprocess.PIPE,
                # The broker reads passwords/recovery phrases directly from its
                # controlling TTY. They never enter this public stdin/stdout path.
                stderr=None,
                check=False,
                timeout=30 if ready_probe else 600,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise NativeBrokerError("native identity operation could not complete") from error
        if completed.returncode != 0 and not (ready_probe and completed.returncode == 78):
            # Never expose an unvalidated stdout buffer, even on native failure.
            raise NativeBrokerError(
                f"native identity operation failed (exit {completed.returncode})"
            )
        return completed.returncode, _native_object(completed.stdout)

    def preflight(self) -> dict[str, object]:
        code, value = self._run(["preflight"], ready_probe=True)
        expected = {
            "schema_version",
            "protection_profile",
            "platform",
            "ready",
            "required_locked_memory_bytes",
            "locked_memory_limit_bytes",
            "reason",
        }
        if (
            set(value) != expected
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["protection_profile"] != PROTECTION_PROFILE
        ):
            raise NativeBrokerError("native broker returned an unsupported preflight contract")
        if type(value["ready"]) is not bool or value["platform"] not in (
            "linux",
            "windows",
            "unsupported",
        ):
            raise NativeBrokerError("native broker returned invalid platform readiness")
        if (
            type(value["required_locked_memory_bytes"]) is not int
            or value["required_locked_memory_bytes"] != REQUIRED_LOCKED_MEMORY_BYTES
        ):
            raise NativeBrokerError(
                "native broker returned an unsupported memory-protection profile"
            )
        limit = value["locked_memory_limit_bytes"]
        if limit is not None and (type(limit) is not int or limit < 0):
            raise NativeBrokerError("native broker returned an invalid locked-memory limit")
        if value["reason"] not in (
            "ready",
            "locked-memory-limit",
            "protected-memory-unavailable",
            "unsupported-platform",
        ):
            raise NativeBrokerError("native broker returned an invalid readiness reason")
        if value["ready"] != (code == 0) or value["ready"] != (value["reason"] == "ready"):
            raise NativeBrokerError("native broker returned inconsistent readiness")
        output: dict[str, object] = {**value, "argon2_memory_bytes": KDF_MEMORY_BYTES}
        if sys.platform == "linux":
            import resource

            soft, hard = resource.getrlimit(resource.RLIMIT_MEMLOCK)
            output["host_memlock_soft_bytes"] = None if soft == resource.RLIM_INFINITY else soft
            output["host_memlock_hard_bytes"] = None if hard == resource.RLIM_INFINITY else hard
            if hard != resource.RLIM_INFINITY and hard < REQUIRED_LOCKED_MEMORY_BYTES:
                output["ready"] = False
                output["reason"] = "locked-memory-limit"
        output["unprotected_fallback"] = False
        output["qualification_scope"] = "native-process-memory; vault ACLs checked per operation"
        return output

    def _require_ready(self) -> None:
        report = self.preflight()
        if report["ready"] is not True:
            limit = report.get("host_memlock_hard_bytes", report["locked_memory_limit_bytes"])
            raise NativeBrokerError(
                "native identity custody is unavailable: "
                f"{report['reason']}; requires {REQUIRED_LOCKED_MEMORY_BYTES} locked bytes "
                f"including the {KDF_MEMORY_BYTES}-byte KDF arena; host limit {limit}; no fallback"
            )

    def public(self, vault: Path, *, expected_signer_hash: str | None = None) -> dict[str, Any]:
        arguments = ["public", "--vault", str(vault)]
        if expected_signer_hash is not None:
            arguments.extend(
                ("--expect-signer", _hash(expected_signer_hash, "expected fingerprint"))
            )
        _, value = self._run(arguments)
        return _descriptor(value, expected_signer_hash)

    def initialize(
        self, vault: Path, policy: Path, *, known_type_refs: frozenset[str] | None = None
    ) -> dict[str, Any]:
        return self._create(
            "init", vault, policy, expected_signer_hash=None, known_type_refs=known_type_refs
        )

    def provision_agent(
        self,
        vault: Path,
        agent_vault: Path,
        *,
        expected_operator_hash: str,
        account_number: int,
        instance_id: str,
        group_id: str,
        group_epoch: int,
    ) -> dict[str, Any]:
        from .provisioning import validate_assignment, verify_membership

        assignment = validate_assignment(
            dict(
                account_number=account_number,
                instance_id=instance_id,
                group_id=group_id,
                group_epoch=group_epoch,
            )
        )
        pin = _hash(expected_operator_hash, "operator fingerprint")
        self._require_ready()
        _, value = self._run(
            [
                "agent-provision",
                "--vault",
                str(vault),
                "--agent-vault",
                str(agent_vault),
                "--expect-signer",
                pin,
                *[
                    item
                    for name, field in assignment.items()
                    for item in ("--" + name.replace("_", "-"), str(field))
                ],
            ]
        )
        checked = verify_membership(value, expected_operator_hash=pin)
        if any(checked[name] != field for name, field in assignment.items()):
            raise NativeBrokerError("native Agent assignment differs from the requested scope")
        return checked

    def agent_allocations(
        self,
        vault: Path,
        *,
        expected_operator_hash: str,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        from .provisioning import validate_assignment

        pin = _hash(expected_operator_hash, "operator fingerprint")
        if (
            type(offset) is not int
            or not 0 <= offset <= 1024
            or type(limit) is not int
            or not 1 <= limit <= 128
        ):
            raise PublicVerificationError("invalid allocation page")
        self._require_ready()
        _, value = self._run(
            [
                "agent-allocations",
                "--vault",
                str(vault),
                "--expect-signer",
                pin,
                "--offset",
                str(offset),
                "--limit",
                str(limit),
            ]
        )
        if set(value) != {
            "schema_version",
            "operator_fingerprint",
            "generation",
            "total",
            "offset",
            "next_offset",
            "has_more",
            "allocations",
        }:
            raise NativeBrokerError("unsupported allocation response")
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["operator_fingerprint"] != pin
        ):
            raise NativeBrokerError("allocation response has a different operator or version")
        for name, minimum, maximum in (
            ("generation", 1, 2**53 - 1),
            ("total", 0, 1024),
            ("offset", 0, 1024),
            ("next_offset", 0, 1024),
        ):
            if type(value[name]) is not int or not minimum <= value[name] <= maximum:
                raise NativeBrokerError("invalid allocation page bounds")
        items = value["allocations"]
        if (
            not isinstance(items, list)
            or len(items) > limit
            or value["offset"] != offset
            or value["next_offset"] != offset + len(items)
            or value["next_offset"] > value["total"]
            or type(value["has_more"]) is not bool
            or value["has_more"] != (value["next_offset"] < value["total"])
        ):
            raise NativeBrokerError("inconsistent allocation page")
        seen: set[object] = set()
        for item in items:
            if not isinstance(item, dict) or set(item) != {
                "account_number",
                "agent_fingerprint",
                "instance_id",
                "group_id",
                "group_epoch",
            }:
                raise NativeBrokerError("invalid allocation record")
            validate_assignment(
                {
                    name: item[name]
                    for name in ("account_number", "instance_id", "group_id", "group_epoch")
                }
            )
            _hash(item["agent_fingerprint"], "Agent fingerprint")
            identities = [
                ("account", item["account_number"]),
                ("agent", item["agent_fingerprint"]),
                ("instance", item["instance_id"]),
            ]
            if any(identity in seen for identity in identities):
                raise NativeBrokerError("duplicate allocation identity")
            seen.update(identities)
        return value

    def agent_public(
        self,
        vault: Path,
        *,
        expected_operator_hash: str,
        expected_agent_hash: str | None = None,
        unlock: bool = False,
    ) -> dict[str, Any]:
        from .provisioning import verify_membership

        pin = _hash(expected_operator_hash, "operator fingerprint")
        arguments = [
            "agent-check" if unlock else "agent-public",
            "--vault",
            str(vault),
            "--expect-operator",
            pin,
        ]
        if expected_agent_hash is not None:
            arguments.extend(("--expect-agent", _hash(expected_agent_hash, "Agent fingerprint")))
        if unlock:
            if expected_agent_hash is None:
                raise NativeBrokerError(
                    "unlock requires an independently selected Agent fingerprint"
                )
            self._require_ready()
        _, value = self._run(arguments)
        return verify_membership(
            value, expected_operator_hash=pin, expected_agent_hash=expected_agent_hash
        )

    def rotate_agent_group(
        self,
        vault: Path,
        agent_vault: Path,
        *,
        expected_operator_hash: str,
        expected_agent_hash: str,
        group_epoch: int,
    ) -> dict[str, Any]:
        from .provisioning import verify_membership

        pin = _hash(expected_operator_hash, "operator fingerprint")
        agent = _hash(expected_agent_hash, "Agent fingerprint")
        if type(group_epoch) is not int or not 0 <= group_epoch < 2**31:
            raise PublicVerificationError("invalid group epoch")
        self._require_ready()
        _, value = self._run(
            [
                "agent-rotate",
                "--vault",
                str(vault),
                "--agent-vault",
                str(agent_vault),
                "--expect-signer",
                pin,
                "--expect-agent",
                agent,
                "--group-epoch",
                str(group_epoch),
            ]
        )
        checked = verify_membership(value, expected_operator_hash=pin, expected_agent_hash=agent)
        if checked["group_epoch"] != group_epoch:
            raise NativeBrokerError("native Agent group epoch differs from the request")
        return checked

    def sign_agent(
        self,
        vault: Path,
        request: dict[str, Any],
        *,
        expected_operator_hash: str,
        expected_agent_hash: str,
    ) -> dict[str, Any]:
        from .provisioning import validate_agent_request, verify_agent_proof

        checked = validate_agent_request(request)
        pin = _hash(expected_operator_hash, "operator fingerprint")
        agent = _hash(expected_agent_hash, "Agent fingerprint")
        self._require_ready()
        _, value = self._run(
            [
                "agent-sign",
                "--vault",
                str(vault),
                "--expect-operator",
                pin,
                "--expect-agent",
                agent,
            ],
            public_input=json.dumps(checked).encode("ascii"),
        )
        result = verify_agent_proof(
            value,
            expected_operator_hash=pin,
            expected_agent_hash=agent,
            group_id=checked["group_id"],
            group_epoch=checked["group_epoch"],
            payload_sha256=checked["payload_sha256"],
        )
        if (
            result["request_id"] != checked["request_id"]
            or result["expires_at"] - result["issued_at"] != checked["validity_seconds"]
        ):
            raise NativeBrokerError(
                "native Agent proof differs from the requested identity or validity"
            )
        return result

    def recover(
        self,
        vault: Path,
        policy: Path,
        *,
        expected_signer_hash: str,
        known_type_refs: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        _hash(expected_signer_hash, "expected fingerprint")
        return self._create(
            "recover",
            vault,
            policy,
            expected_signer_hash=expected_signer_hash,
            known_type_refs=known_type_refs,
        )

    def _create(
        self,
        operation: str,
        vault: Path,
        policy: Path,
        *,
        expected_signer_hash: str | None,
        known_type_refs: frozenset[str] | None,
    ) -> dict[str, Any]:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(policy, flags)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise PublicVerificationError("signing policy must be a regular file")
            raw = stream.read(MAX_PUBLIC_DOCUMENT_BYTES + 1)
        if len(raw) > MAX_PUBLIC_DOCUMENT_BYTES:
            raise PublicVerificationError("signing policy exceeds size limit")
        supplied = strict_json(raw)
        if not isinstance(supplied, dict):
            raise PublicVerificationError("signing policy must be a JSON object")
        checked = validate_signing_policy(supplied, known_type_refs=known_type_refs)
        self._require_ready()
        # Pass an immutable checked policy snapshot; never read or create the
        # vault's final directory. Native code applies its private ACL contract.
        with tempfile.TemporaryDirectory(prefix="potbot-public-policy-") as directory:
            snapshot = Path(directory) / "policy.json"
            snapshot.write_text(
                json.dumps(checked, sort_keys=True, separators=(",", ":")), encoding="ascii"
            )
            arguments = [operation, "--vault", str(vault), "--policy", str(snapshot)]
            if expected_signer_hash is not None:
                arguments.extend(("--expect-signer", expected_signer_hash))
            _, output = self._run(arguments)
        return _descriptor(output, expected_signer_hash)

    def sign(
        self,
        vault: Path,
        request: Mapping[str, object],
        *,
        expected_signer_hash: str,
        known_type_refs: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        checked, policy = _request_policy(request, expected_signer_hash, known_type_refs)
        self._require_ready()
        _, bundle = self._run(
            ["sign", "--vault", str(vault), "--expect-signer", expected_signer_hash],
            public_input=json.dumps(checked, sort_keys=True, separators=(",", ":")).encode("ascii"),
        )
        verified = verify_signature_bundle_digest(
            bundle, str(checked["payload_sha256"]), policy=policy
        )
        if (
            verified.request_id != checked["request_id"]
            or verified.expires_at - verified.issued_at != checked["validity_seconds"]
        ):
            raise NativeBrokerError("native signature does not match the exact authorized request")
        return bundle

    def _lifecycle(
        self,
        operation: str,
        vault: Path,
        *,
        expected_signer_hash: str | None,
        output: Path | None = None,
    ) -> dict[str, Any]:
        if operation not in ("recovery", "change-password", "backup"):
            raise NativeBrokerError("unsupported native lifecycle operation")
        arguments = [operation, "--vault", str(vault)]
        if operation == "backup" and expected_signer_hash is not None:
            self.public(vault, expected_signer_hash=expected_signer_hash)
        if operation != "backup":
            _hash(expected_signer_hash, "expected fingerprint")
            self._require_ready()
            arguments.extend(("--expect-signer", str(expected_signer_hash)))
        if output is not None:
            arguments.extend(("--output", str(output)))
        _, value = self._run(arguments)
        if (
            set(value) != {"schema_version", "operation", "status", "identity"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["operation"] != operation
            or value["status"] != "complete"
        ):
            raise NativeBrokerError("native broker returned an invalid lifecycle result")
        if not isinstance(value["identity"], dict):
            raise NativeBrokerError("native lifecycle result lacks a public identity")
        value["identity"] = _descriptor(value["identity"], expected_signer_hash)
        return value

    def recovery(self, vault: Path, *, expected_signer_hash: str) -> dict[str, Any]:
        """Native-only explicit TTY display; no recovery phrase traverses Python."""
        return self._lifecycle("recovery", vault, expected_signer_hash=expected_signer_hash)

    def change_password(self, vault: Path, *, expected_signer_hash: str) -> dict[str, Any]:
        return self._lifecycle("change-password", vault, expected_signer_hash=expected_signer_hash)

    def backup(
        self, vault: Path, output: Path, *, expected_signer_hash: str | None = None
    ) -> dict[str, Any]:
        return self._lifecycle(
            "backup", vault, expected_signer_hash=expected_signer_hash, output=output
        )
