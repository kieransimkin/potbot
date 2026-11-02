"""Bounded bootstrap verification; no Syn code executes before authentication.

This narrow distribution adapter implements the documented release envelope.
The installed official syn-client performs the authoritative contract check again.
It implements no node, identity, transport, consensus or private-key operation.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError

REPOSITORY = "Synsphere/syn"
DOCUMENT_LIMIT = 65_536
ARTIFACT_LIMIT = 536_870_912
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
TOKEN = re.compile(r"[a-z][a-z0-9._/-]{0,127}\Z")
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def document(raw: bytes, *, canonical_only: bool = False) -> dict[str, Any]:
    if not 0 < len(raw) <= DOCUMENT_LIMIT:
        raise InvalidInputError("Syn release document exceeds its bound")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise InvalidInputError("Duplicate Syn release JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)),
        )
        if type(value) is not dict or (canonical_only and canonical(value) != raw):
            raise ValueError("not a canonical object")
    except (ValueError, UnicodeError) as error:
        raise InvalidInputError("Invalid Syn release JSON") from error
    return value


def fields(value: object, expected: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        raise InvalidInputError("Missing or unknown Syn release fields")
    return value


def digest(value: object) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise InvalidInputError("Expected an exact lowercase SHA-256")
    return value


def version(value: object) -> tuple[int, ...]:
    if not isinstance(value, str) or len(value) > 64 or not VERSION.fullmatch(value):
        raise InvalidInputError("Select an exact stable Syn version; never latest or a branch")
    return tuple(int(part) for part in value.split("."))


def strings(value: object) -> tuple[str, ...]:
    if (
        type(value) is not list
        or len(value) > 32
        or any(not isinstance(x, str) or not 0 < len(x) <= 128 for x in value)
        or value != sorted(set(value))
    ):
        raise InvalidInputError("Expected a bounded sorted unique string list")
    return tuple(value)


@dataclass(frozen=True)
class Policy:
    """Operator-owned trust and compatibility selection, never from the download."""

    value: dict[str, Any]

    @classmethod
    def parse(cls, raw: bytes) -> Policy:
        supplied = document(raw)
        supplied.setdefault("dependency_source_builds", False)
        if type(supplied["dependency_source_builds"]) is not bool:
            raise InvalidInputError("Dependency source-build approval must be an explicit boolean")
        supplied.setdefault("dependency_cache_sha256", None)
        if supplied["dependency_cache_sha256"] is not None:
            digest(supplied["dependency_cache_sha256"])
        value = fields(
            supplied,
            {
                "profile",
                "repository",
                "version",
                "source_commit",
                "manifest_sha256",
                "release_key_sha256",
                "public_key",
                "minimum_version",
                "revoked_keys",
                "revoked_manifests",
                "component",
                "mode",
                "python_version",
                "required_profiles",
                "required_capabilities",
                "dependency_cache_sha256",
                "dependency_source_builds",
            },
        )
        if value["profile"] != "potbot.syn-install-policy/1" or value["repository"] != REPOSITORY:
            raise ScopeDeniedError("Syn installation requires the canonical upstream trust policy")
        if version(value["version"]) < version(value["minimum_version"]):
            raise ScopeDeniedError("Selected Syn version violates the operator downgrade floor")
        if not isinstance(value["source_commit"], str) or not COMMIT.fullmatch(
            value["source_commit"]
        ):
            raise InvalidInputError("Expected a full released Syn source SHA")
        for key in ("manifest_sha256", "release_key_sha256", "public_key"):
            digest(value[key])
        if (
            hashlib.sha256(bytes.fromhex(value["public_key"])).hexdigest()
            != value["release_key_sha256"]
        ):
            raise ScopeDeniedError("Syn authority key differs from the independent fingerprint")
        for name in ("revoked_keys", "revoked_manifests"):
            for item in strings(value[name]):
                digest(item)
        if (
            value["release_key_sha256"] in value["revoked_keys"]
            or value["manifest_sha256"] in value["revoked_manifests"]
        ):
            raise ScopeDeniedError("Selected Syn release or authority is revoked")
        if (
            not isinstance(value["component"], str)
            or value["component"] not in {"client", "runtime"}
            or not isinstance(value["python_version"], str)
            or value["python_version"] not in {"3.12", "3.13"}
        ):
            raise InvalidInputError("Unsupported Syn component or Python selection")
        if (value["component"] == "client" and value["mode"] is not None) or (
            value["component"] == "runtime"
            and (
                not isinstance(value["mode"], str)
                or value["mode"] not in {"service", "operator", "agent"}
            )
        ):
            raise InvalidInputError("Select an explicit runtime mode; a client is not a node")
        strings(value["required_profiles"])
        strings(value["required_capabilities"])
        return cls(value)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical(self.value)).hexdigest()

    @property
    def manifest_sha256(self) -> str:
        return str(self.value["manifest_sha256"])

    @property
    def tag(self) -> str:
        return "v" + str(self.value["version"])


@dataclass(frozen=True)
class Asset:
    role: str
    name: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Release:
    policy: Policy
    raw: bytes
    signature: bytes
    runtime: dict[str, Any]
    assets: tuple[Asset, ...]

    def compatibility(self, platform: str, python_version: str) -> None:
        selected = self.policy.value
        if (
            python_version != selected["python_version"]
            or python_version not in self.runtime["python_versions"]
            or platform not in self.runtime[selected["component"] + "_platforms"]
        ):
            raise ScopeDeniedError("Syn release does not support the selected host or Python")
        if (
            not set(selected["required_profiles"]) <= set(self.runtime["api_profiles"])
            or not set(selected["required_capabilities"]) <= set(self.runtime["capabilities"])
            or (selected["mode"] is not None and selected["mode"] not in self.runtime["node_modes"])
        ):
            raise ScopeDeniedError("Syn release lacks a required protocol, capability or node mode")


def verify_manifest(raw: bytes, signature: bytes, policy: Policy) -> Release:
    if hashlib.sha256(raw).hexdigest() != policy.manifest_sha256:
        raise ScopeDeniedError("Syn manifest differs from the independent selection")
    envelope = fields(
        document(signature, canonical_only=True),
        {"profile", "manifest_sha256", "release_key_sha256", "signature"},
    )
    if (
        envelope["profile"] != "syn.release-signature/1"
        or envelope["manifest_sha256"] != policy.manifest_sha256
        or envelope["release_key_sha256"] != policy.value["release_key_sha256"]
        or not isinstance(envelope["signature"], str)
        or not re.fullmatch(r"[0-9a-f]{128}", envelope["signature"])
    ):
        raise ScopeDeniedError("Syn release signature envelope differs from selected trust")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as error:
        raise CapabilityUnavailableError(
            "Install Potbot's security extra to verify Syn releases"
        ) from error
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(policy.value["public_key"])).verify(
            bytes.fromhex(envelope["signature"]), b"syn.release-manifest/1\0" + raw
        )
    except (InvalidSignature, ValueError) as error:
        raise ScopeDeniedError("Syn publisher signature verification failed") from error
    value = fields(
        document(raw, canonical_only=True),
        {"profile", "repository", "version", "tag", "source_commit", "runtime", "artifacts"},
    )
    if any(
        value[name] != expected
        for name, expected in {
            "profile": "syn.release-manifest/1",
            "repository": REPOSITORY,
            "version": policy.value["version"],
            "source_commit": policy.value["source_commit"],
            "tag": policy.tag,
        }.items()
    ):
        raise ScopeDeniedError("Signed Syn release identity differs from independent pins")
    runtime = fields(
        value["runtime"],
        {
            "profile",
            "runtime_version",
            "client_version",
            "python_versions",
            "runtime_platforms",
            "client_platforms",
            "node_modes",
            "api_profiles",
            "capabilities",
            "database_revision",
        },
    )
    if (
        runtime["profile"] != "syn.runtime-contract/1"
        or runtime["runtime_version"] != value["version"]
        or runtime["client_version"] != value["version"]
        or not isinstance(runtime["database_revision"], str)
        or not re.fullmatch(r"[0-9]{8}_[0-9]{4}", runtime["database_revision"])
    ):
        raise InvalidInputError("Signed Syn runtime contract has an unsupported identity")
    for key in (
        "python_versions",
        "runtime_platforms",
        "client_platforms",
        "node_modes",
        "api_profiles",
        "capabilities",
    ):
        if not strings(runtime[key]):
            raise InvalidInputError("Syn runtime contract requires nonempty declarations")
    v = str(value["version"])
    names = {
        "client-sdist": f"syn_client-{v}.tar.gz",
        "client-wheel": f"syn_client-{v}-py3-none-any.whl",
        "dependency-lock": f"syn-runtime-requirements-{v}.txt",
        "runtime-sdist": f"syn-{v}.tar.gz",
        "runtime-wheel": f"syn-{v}-py3-none-any.whl",
    }
    if type(value["artifacts"]) is not list or len(value["artifacts"]) != 5:
        raise InvalidInputError("Syn requires exactly five signed package payloads")
    allowed = {
        "python_versions": {"3.12", "3.13"},
        "runtime_platforms": {"darwin-arm64", "darwin-x86_64", "linux-x86_64", "windows-x86_64"},
        "client_platforms": {"darwin-arm64", "darwin-x86_64", "linux-x86_64", "windows-x86_64"},
        "node_modes": {"service", "operator", "agent"},
    }
    for name, choices in allowed.items():
        if not set(runtime[name]) <= choices:
            raise InvalidInputError("Unknown Syn compatibility declaration")
    for name in ("api_profiles", "capabilities"):
        if any(not TOKEN.fullmatch(item) for item in runtime[name]):
            raise InvalidInputError("Invalid Syn capability or protocol token")
    assets = []
    for item in value["artifacts"]:
        item = fields(item, {"role", "filename", "sha256", "size"})
        if (
            not isinstance(item["role"], str)
            or item["role"] not in names
            or item["filename"] != names[item["role"]]
            or type(item["size"]) is not int
            or not 0 < item["size"] <= ARTIFACT_LIMIT
        ):
            raise InvalidInputError("Invalid Syn package name, role or size")
        assets.append(Asset(item["role"], item["filename"], digest(item["sha256"]), item["size"]))
    if [x.role for x in assets] != sorted(names):
        raise InvalidInputError("Syn package roles must be exact, sorted and unique")
    return Release(policy, raw, signature, runtime, tuple(assets))


def inspect_wheel(path: Path) -> None:
    """Check archive structure without unpacking or importing any package code."""
    try:
        with zipfile.ZipFile(path) as wheel:
            names: set[str] = set()
            expanded = 0
            for item in wheel.infolist():
                original = item.orig_filename
                parts = PurePosixPath(original).parts
                expanded += item.file_size
                if (
                    not parts
                    or original.startswith("/")
                    or "\\" in original
                    or "\0" in original
                    or PurePosixPath(original).as_posix() != original.rstrip("/")
                    or any(part in {".", ".."} or ":" in part for part in parts)
                    or item.filename in names
                    or item.flag_bits & 1
                    or stat.S_ISLNK(item.external_attr >> 16)
                    or expanded > 134_217_728
                ):
                    raise ScopeDeniedError("Unsafe Syn wheel member or expansion size")
                names.add(item.filename)
    except (OSError, zipfile.BadZipFile) as error:
        raise InvalidInputError("Syn wheel is unreadable") from error


def inspect_requirements(path: Path) -> None:
    """Accept the signed uv-export form, with no nested files or index directives."""
    if path.stat().st_size > 1_048_576:
        raise InvalidInputError("Syn dependency lock exceeds its bound")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError as error:
        raise InvalidInputError("Syn dependency lock is not UTF-8") from error
    count = 0
    for line in text.splitlines():
        line = line.strip().removesuffix("\\").strip()
        if not line or line.startswith("#"):
            continue
        if re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", line):
            continue
        if "://" in line or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?==[A-Za-z0-9.+!_-]+(?:\s*;[^\r\n]*)?",
            line,
        ):
            raise ScopeDeniedError(
                "Syn dependency lock must contain exact hashed pins, without URL or option overrides"
            )
        count += 1
    if not 0 < count <= 1024:
        raise InvalidInputError("Syn dependency lock has an invalid requirement count")
