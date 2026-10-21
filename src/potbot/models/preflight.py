"""Offline, read-only checks for an explicitly pinned local inference host.

No backend or model code is imported, no weights are downloaded, no provider is
contacted and no credentials are read. Passing checks means only that the local
configuration is ready for a separately supervised loading check. It never
qualifies a model or asserts that loading/inference have succeeded.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from potbot.domain.errors import InvalidInputError, SchemaViolationError
from potbot.domain.serialization import content_hash
from potbot.models.envelopes import strict_json
from potbot.models.profiles import conformance_inventory, get_model_profile
from potbot.models.registry import DeploymentSpec

_DIGEST = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_KINDS = {"weights", "tokenizer", "template", "configuration"}


def _relative_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or "\\" in value
        or ":" in value
        or "\0" in value
    ):
        raise InvalidInputError("model paths must be bounded relative POSIX paths")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or str(path) != value
        or any(p in {".", ".."} for p in path.parts)
    ):
        raise InvalidInputError("model paths must be bounded relative POSIX paths")
    return value


@dataclass(frozen=True, slots=True)
class PinnedFile:
    path: str
    sha256: str
    bytes: int
    kind: str

    def __post_init__(self) -> None:
        _relative_path(self.path)
        if not isinstance(self.sha256, str) or not _DIGEST.fullmatch(self.sha256):
            raise InvalidInputError("every model file requires an expected SHA256 digest")
        if type(self.bytes) is not int or self.bytes <= 0:
            raise InvalidInputError("every model file requires an expected positive byte size")
        if not isinstance(self.kind, str) or self.kind not in _KINDS:
            raise InvalidInputError("unsupported model file purpose")

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "bytes": self.bytes, "kind": self.kind}


def weights_inventory_digest(files: tuple[PinnedFile, ...]) -> str:
    """SHA256 of canonical sorted weight-file inventory, used by this host format.

    This identity covers every shard name, expected byte size and content digest.
    Actual bytes are rehashed separately by :func:`preflight_local`.
    """
    return content_hash(
        [
            file.to_dict()
            for file in sorted(files, key=lambda file: file.path)
            if file.kind == "weights"
        ]
    )


@dataclass(frozen=True, slots=True)
class LocalHostProfile:
    deployment: DeploymentSpec
    model_revision: str
    model_directory: str
    files: tuple[PinnedFile, ...]
    backend_distribution: str
    context_memory_bytes: int
    runtime_overhead_bytes: int
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise InvalidInputError("unsupported local host profile schema")
        if not isinstance(self.deployment, DeploymentSpec):
            raise InvalidInputError("an exact deployment is required")
        get_model_profile(self.deployment.canonical_model)
        if not isinstance(self.model_revision, str) or not _REVISION.fullmatch(self.model_revision):
            raise InvalidInputError("model revision must be an immutable commit or digest")
        _relative_path(self.model_directory)
        if PurePosixPath(self.model_directory).parts[0] != "models":
            raise InvalidInputError("local inference assets must be below project models/")
        if (
            not isinstance(self.files, tuple)
            or not 1 <= len(self.files) <= 4096
            or any(not isinstance(file, PinnedFile) for file in self.files)
            or len({file.path for file in self.files}) != len(self.files)
        ):
            raise InvalidInputError("a bounded unique pinned model-file inventory is required")
        kinds = {file.kind for file in self.files}
        if kinds != _KINDS or sum(file.kind == "template" for file in self.files) != 1:
            raise InvalidInputError(
                "weights, tokenizer, configuration and one template are required"
            )
        if not isinstance(self.backend_distribution, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", self.backend_distribution
        ):
            raise InvalidInputError("an installed backend distribution name is required")
        if any(
            type(value) is not int or value < 0
            for value in (self.context_memory_bytes, self.runtime_overhead_bytes)
        ):
            raise InvalidInputError("context and runtime memory estimates must be explicit")
        object.__setattr__(self, "files", tuple(sorted(self.files, key=lambda file: file.path)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "deployment": self.deployment.to_dict(),
            "model_revision": self.model_revision,
            "model_directory": self.model_directory,
            "files": [file.to_dict() for file in self.files],
            "backend_distribution": self.backend_distribution,
            "context_memory_bytes": self.context_memory_bytes,
            "runtime_overhead_bytes": self.runtime_overhead_bytes,
        }

    @property
    def fingerprint(self) -> str:
        return content_hash(self.to_dict())

    @classmethod
    def from_dict(cls, document: Any) -> LocalHostProfile:
        required = {
            "schema_version",
            "deployment",
            "model_revision",
            "model_directory",
            "files",
            "backend_distribution",
            "context_memory_bytes",
            "runtime_overhead_bytes",
        }
        if not isinstance(document, dict) or set(document) != required:
            raise InvalidInputError("local host profile fields are missing or unsupported")
        deployment = document["deployment"]
        deployment_fields = {
            "schema_version",
            "canonical_model",
            "weights_digest",
            "backend",
            "backend_version",
            "quantization",
            "tokenizer_revision",
            "chat_template_digest",
            "output_adapter",
            "tool_protocol",
            "usable_context_tokens",
            "max_output_tokens",
            "prompt_versions",
            "roles",
            "response_schemas",
            "resource_profile",
            "cancellation",
        }
        if not isinstance(deployment, dict) or set(deployment) != deployment_fields:
            raise InvalidInputError("exact deployment fields are missing or unsupported")
        if (
            not isinstance(deployment["prompt_versions"], dict)
            or not isinstance(deployment["roles"], list)
            or not isinstance(deployment["response_schemas"], list)
            or not isinstance(document["files"], list)
            or any(
                not isinstance(file, dict) or set(file) != {"path", "sha256", "bytes", "kind"}
                for file in document["files"]
            )
        ):
            raise InvalidInputError("invalid deployment or file inventory collections")
        fields = dict(deployment)
        fields["resource_profile_json"] = json.dumps(fields.pop("resource_profile"))
        fields["prompt_versions"] = tuple(fields["prompt_versions"].items())
        fields["roles"] = tuple(fields["roles"])
        fields["response_schemas"] = tuple(fields["response_schemas"])
        return cls(
            deployment=DeploymentSpec(**fields),
            model_revision=document["model_revision"],
            model_directory=document["model_directory"],
            files=tuple(PinnedFile(**file) for file in document["files"]),
            backend_distribution=document["backend_distribution"],
            context_memory_bytes=document["context_memory_bytes"],
            runtime_overhead_bytes=document["runtime_overhead_bytes"],
            schema_version=document["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class HostObservation:
    """Read-only observations; an injected observation must identify its origin."""

    backend_distribution: str
    backend_version: str | None
    device: str
    available_memory_bytes: int | None
    origin: str = "local_os"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.backend_distribution, str)
            or not self.backend_distribution
            or self.backend_version is not None
            and (not isinstance(self.backend_version, str) or not self.backend_version)
            or not isinstance(self.device, str)
            or not self.device
            or self.available_memory_bytes is not None
            and (type(self.available_memory_bytes) is not int or self.available_memory_bytes < 0)
            or self.origin not in {"local_os", "operator_observation", "fixture"}
        ):
            raise InvalidInputError("invalid host observation")

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend_distribution": self.backend_distribution,
            "backend_version": self.backend_version,
            "device": self.device,
            "available_memory_bytes": self.available_memory_bytes,
            "origin": self.origin,
        }


def inspect_local_host(profile: LocalHostProfile) -> HostObservation:
    """Inspect installed package metadata and available CPU RAM without imports.

    GPU memory requires an explicit observation from a later device adapter.
    Unknown memory is retained as unknown instead of guessing a model will fit.
    """
    try:
        version = importlib.metadata.version(profile.backend_distribution)
    except importlib.metadata.PackageNotFoundError:
        version = None
    available: int | None = None
    try:
        values = Path("/proc/meminfo").read_text(encoding="ascii")
        match = re.search(r"^MemAvailable:\s+(\d+) kB$", values, re.MULTILINE)
        if match:
            available = int(match.group(1)) * 1024
    except (OSError, UnicodeError):
        pass
    if available is None and hasattr(os, "sysconf"):
        try:
            pages, page_size = os.sysconf("SC_AVPHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
            if pages >= 0 and page_size > 0:
                available = pages * page_size
        except (OSError, ValueError):
            pass
    return HostObservation(profile.backend_distribution, version, "cpu", available)


@dataclass(frozen=True, slots=True)
class PreflightBlocker:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class PreflightReport:
    canonical_model: str | None
    deployment_fingerprint: str | None
    host_profile_fingerprint: str | None
    blockers: tuple[PreflightBlocker, ...]
    checked_files: int
    checked_bytes: int
    required_memory_bytes: int | None
    observation: HostObservation | None
    conformance_requirements: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        return "blocked" if self.blockers else "ready_for_load_check"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": self.status,
            "canonical_model": self.canonical_model,
            "deployment_fingerprint": self.deployment_fingerprint,
            "host_profile_fingerprint": self.host_profile_fingerprint,
            "blockers": [blocker.to_dict() for blocker in self.blockers],
            "checked_files": self.checked_files,
            "checked_bytes": self.checked_bytes,
            "required_memory_bytes": self.required_memory_bytes,
            "host_observation": self.observation.to_dict() if self.observation else None,
            "qualified": False,
            "loading_tested": False,
            "generation_tested": False,
            "network_used": False,
            "conformance_requirements": list(self.conformance_requirements),
        }


def _read_verified_file(directory: Path, file: PinnedFile) -> bytes | None:
    path = directory / file.path
    if path.resolve() != path or not stat.S_ISREG(path.lstat().st_mode):
        raise InvalidInputError("model files must be ordinary files without links")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != file.bytes:
            raise InvalidInputError("model file byte size does not match its pinned inventory")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
        if digest != file.sha256 or (before.st_size, before.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise InvalidInputError("model file bytes do not match their pinned inventory")
        if file.kind != "template":
            return None
        if file.bytes > 1024 * 1024:
            raise InvalidInputError("chat template metadata exceeds its supported size")
        stream.seek(0)
        content = stream.read()
        if hashlib.sha256(content).hexdigest() != file.sha256:
            raise InvalidInputError("chat template metadata changed while being inspected")
        return content


def preflight_local(
    profile: LocalHostProfile,
    *,
    project_root: Path,
    observation: HostObservation | None = None,
) -> PreflightReport:
    """Rehash pinned local artifacts and compare explicit host/resource facts."""
    blockers: list[PreflightBlocker] = []

    def block(code: str, message: str) -> None:
        blockers.append(PreflightBlocker(code, message))

    deployment = profile.deployment
    adapter = get_model_profile(deployment.canonical_model)
    if adapter.role not in deployment.roles:
        block("role_mismatch", "The deployment does not support its default model role.")
    if deployment.weights_digest != weights_inventory_digest(profile.files):
        block(
            "weights_identity_mismatch", "The pinned weight inventory differs from the deployment."
        )
    resource = json.loads(deployment.resource_profile_json)
    required_memory: int | None = None
    if (
        resource["measurement"] == "unknown"
        or resource["memory_bytes"] == 0
        or profile.context_memory_bytes == 0
        or profile.runtime_overhead_bytes == 0
    ):
        block(
            "resource_profile_incomplete",
            "Explicit weights, context and runtime memory are required.",
        )
    else:
        required_memory = (
            resource["memory_bytes"] + profile.context_memory_bytes + profile.runtime_overhead_bytes
        )
    observed = observation if observation is not None else inspect_local_host(profile)
    if observed.backend_distribution != profile.backend_distribution:
        block("backend_identity_mismatch", "The observed backend distribution is different.")

    def normalize(value: str) -> str:
        return re.sub(r"[-_.]+", "-", value).lower()

    if normalize(deployment.backend) != normalize(profile.backend_distribution):
        block(
            "backend_identity_mismatch", "The deployment and installed backend identities differ."
        )
    if observed.backend_version is None:
        block("backend_unavailable", "The configured backend distribution is not installed.")
    elif observed.backend_version != deployment.backend_version:
        block(
            "backend_version_mismatch", "The observed backend version differs from the deployment."
        )
    if resource["device"] != observed.device:
        block("device_unavailable", "No matching device observation is available.")
    if observed.available_memory_bytes is None:
        block("host_memory_unknown", "Available device memory has not been observed.")
    elif required_memory is not None and required_memory > observed.available_memory_bytes:
        block(
            "insufficient_memory", "Observed device memory is below the configured loading budget."
        )

    directory = project_root.resolve() / profile.model_directory
    checked_files = checked_bytes = 0
    if directory.resolve() != directory or not directory.is_dir():
        block(
            "model_directory_unavailable", "The project-local model directory is absent or linked."
        )
    else:
        for file in profile.files:
            try:
                template_bytes = _read_verified_file(directory, file)
                checked_files += 1
                checked_bytes += file.bytes
                if template_bytes is not None:
                    template = template_bytes.decode("utf-8")
                    if file.path.endswith(".json"):
                        value = json.loads(template)
                        if not isinstance(value, dict) or not isinstance(
                            value.get("chat_template"), str
                        ):
                            raise InvalidInputError("one explicit chat template must be selected")
                        template = value["chat_template"]
                    if (
                        hashlib.sha256(template.encode("utf-8")).hexdigest()
                        != deployment.chat_template_digest
                    ):
                        block(
                            "template_identity_mismatch",
                            "The local chat template differs from the deployment.",
                        )
            except (OSError, UnicodeError, ValueError):
                block(
                    "model_file_unavailable",
                    "A pinned model file is missing, invalid or has changed.",
                )
    return PreflightReport(
        deployment.canonical_model,
        deployment.fingerprint,
        profile.fingerprint,
        tuple(blockers),
        checked_files,
        checked_bytes,
        required_memory,
        observed,
        conformance_inventory(adapter.role),
    )


def preflight_document(
    document: Any,
    *,
    project_root: Path,
    observation: HostObservation | None = None,
) -> PreflightReport:
    """Return a sanitized blocker for incomplete/unpinned host configuration.

    File/CLI callers should pass raw bytes or text so duplicate keys, non-finite
    numbers, oversized integers and excessive JSON nesting are rejected before
    decoding. The dictionary form remains useful for explicitly built profiles.
    """
    try:
        if isinstance(document, bytes | str):
            document = strict_json(document, max_output_bytes=1024 * 1024)
        profile = LocalHostProfile.from_dict(document)
    except (TypeError, ValueError, AttributeError, RecursionError, SchemaViolationError):
        return PreflightReport(
            None,
            None,
            None,
            (
                PreflightBlocker(
                    "invalid_host_profile",
                    "Exact host and deployment fields are incomplete or invalid.",
                ),
            ),
            0,
            0,
            None,
            None,
        )
    return preflight_local(profile, project_root=project_root, observation=observation)
