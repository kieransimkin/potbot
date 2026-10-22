"""Non-executable, explicitly typed tool-interface and invocation contracts.

These are *checked descriptions*, not installed tool handlers. In particular:
- constructing a descriptor never grants execution authority;
- an invocation intent is not an authorization receipt;
- an observation is evidence of a reported result, not independent verification;
- an immutable receipt is a content identity, not a signature or attestation.

Only the controller-owned ToolGateway, its existing workspace authorization,
process supervisor, budget ledger and durable execution store may dispatch.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, ClassVar, Self
from uuid import UUID

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.syn import SynRef, parse_type_ref, type_identity

_TOOL_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,79}\Z", re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ALLOWED_PROTOCOLS = frozenset({"mcp", "cli", "rest", "graphql", "grpc", "sql", "browser", "solver"})
_EFFECTS = frozenset({"read_only", "idempotent_write", "non_idempotent_write", "destructive"})
_OUTCOMES = frozenset({"succeeded", "failed", "indeterminate"})
MAX_CONTRACT_BYTES = 128 * 1024
MAX_TOOL_OUTPUT_BYTES = 16 * 1024 * 1024

# Existing, *opt-in* hierarchy profile Types. Do not create a second ontology.
TOOL_DEFINITION_TYPE = "synny://type/tool-definition"
MCP_TOOL_DEFINITION_TYPE = "synny://type/mcp-tool-definition"
TOOL_INTERFACE_RECORD_TYPE = "synny://type/tool-interface-record"
TOOL_INVOCATION_TYPE = "synny://type/tool-invocation"


def _fields(raw: object, required: frozenset[str]) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != required:
        raise InvalidInputError("Tool contract fields do not match schema v1")
    return raw


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise InvalidInputError(f"{label} must be lowercase SHA-256")
    return value


def _text(value: object, label: str, maximum: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(c) < 32 for c in value)
    ):
        raise InvalidInputError(f"{label} must be bounded, nonempty UTF-8 text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InvalidInputError(f"{label} must contain valid UTF-8 text") from error
    return value


def _instance_ref(value: object, *, kind: str = "thing") -> str:
    ref = SynRef(value)
    if ref.kind != kind:
        raise InvalidInputError(f"Tool instance identity must be syn://{kind}/...")
    return str(ref)


def _type_ref(value: object) -> str:
    return str(parse_type_ref(value))


def _uuid(value: object, label: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            parsed = UUID(value)
        except (ValueError, AttributeError) as error:
            raise InvalidInputError(f"{label} must be a UUID") from error
        if str(parsed) == value:
            return parsed
    raise InvalidInputError(f"{label} must be a UUID")


def _positive(value: object, label: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise InvalidInputError(f"{label} must be an integer from 1 to {maximum}")
    return value


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise InvalidInputError("Tool JSON schema has duplicate object keys")
        out[key] = value
    return out


def _schema(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidInputError("Tool schema JSON must be bounded text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InvalidInputError("Tool schema JSON must be valid UTF-8") from error
    if size > MAX_CONTRACT_BYTES:
        raise InvalidInputError("Tool schema JSON must be bounded text")
    try:
        parsed = json.loads(value, object_pairs_hook=_unique, parse_constant=lambda _: None)
    except (ValueError, TypeError, RecursionError) as error:
        raise InvalidInputError("Tool schema is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise InvalidInputError("Tool JSON schema must describe an object")
    if canonical_json(parsed) != value:
        raise InvalidInputError("Tool JSON schema must use canonical JSON encoding")
    # JSON Schema validity and evaluation belong to the installed protocol adapter.
    return value


def _scopes(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, tuple)
        or len(value) > 64
        or any(not isinstance(v, str) or not _TOOL_NAME.fullmatch(v) for v in value)
        or len(set(value)) != len(value)
    ):
        raise InvalidInputError("Tool permission scopes must be distinct bounded strings")
    return value


@dataclass(frozen=True, slots=True)
class ToolCapabilityDescriptor:
    """Untrusted public description, never an executable binding."""

    name: str
    definition_type_ref: str
    input_type_ref: str
    output_type_ref: str
    protocol: str
    effect_class: str
    input_schema_json: str
    output_schema_json: str
    description: str
    schema_version: int = 1

    _FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "name", "definition_type_ref", "input_type_ref",
        "output_type_ref", "protocol", "effect_class", "input_schema_json",
        "output_schema_json", "description",
    })

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise InvalidInputError("Unsupported tool capability schema version")
        if not isinstance(self.name, str) or _TOOL_NAME.fullmatch(self.name) is None:
            raise InvalidInputError("Tool capability name must be a bounded identifier")
        for name in ("definition_type_ref", "input_type_ref", "output_type_ref"):
            _type_ref(getattr(self, name))
        if self.protocol not in _ALLOWED_PROTOCOLS or self.effect_class not in _EFFECTS:
            raise InvalidInputError("Tool transport or side effect classification is invalid")
        if self.protocol == "mcp" and type_identity(self.definition_type_ref) != type_identity(MCP_TOOL_DEFINITION_TYPE):
            raise InvalidInputError("MCP tools require the registered MCP tool-definition Type")
        _schema(self.input_schema_json)
        _schema(self.output_schema_json)
        _text(self.description, "tool description", 4096)
        if len(canonical_json(self.to_dict()).encode("utf-8")) > MAX_CONTRACT_BYTES:
            raise InvalidInputError("Tool capability descriptor is too large")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "definition_type_ref": self.definition_type_ref,
            "input_type_ref": self.input_type_ref,
            "output_type_ref": self.output_type_ref,
            "protocol": self.protocol,
            "effect_class": self.effect_class,
            "input_schema_json": self.input_schema_json,
            "output_schema_json": self.output_schema_json,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, raw: object) -> Self:
        return cls(**_fields(raw, cls._FIELDS))

    @property
    def input_schema(self) -> dict[str, Any]:
        return json.loads(self.input_schema_json)

    @property
    def output_schema(self) -> dict[str, Any]:
        return json.loads(self.output_schema_json)

    @property
    def fingerprint(self) -> str:
        return content_hash(self.to_dict())

    def require_registered_types(self) -> None:
        """Resolve all references against the existing *selected* SYN hierarchy.

        This is catalog validation, not admission. It does not install handlers
        or grant authority. Phase 7's unimported Types are deliberately absent.
        """
        from potbot.semantics.catalog import merge_types, type_ancestors
        from potbot.semantics.hierarchy import profile_snapshot

        merged = merge_types(profile_snapshot())
        available = {type_identity(row.ref): row for row in merged}
        for name in ("definition_type_ref", "input_type_ref", "output_type_ref"):
            if type_identity(getattr(self, name)) not in available:
                raise InvalidInputError(f"Unregistered tool contract Type: {name}")
        ancestry = type_ancestors(merged)
        ancestors = ancestry[str(available[type_identity(self.definition_type_ref)].ref)]
        if not any(type_identity(value) == type_identity(TOOL_INTERFACE_RECORD_TYPE) for value in ancestors):
            raise InvalidInputError("Tool definition Type does not descend from tool-interface-record")


@dataclass(frozen=True, slots=True)
class ToolImplementationDescriptor:
    """Version-pinned implementation identity; supply-chain check not implied."""

    implementation_ref: str
    capability_hash: str
    package_digest: str
    publisher: str
    version: str
    schema_version: int = 1

    _FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "implementation_ref", "capability_hash", "package_digest",
        "publisher", "version",
    })

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise InvalidInputError("Unsupported implementation contract version")
        _instance_ref(self.implementation_ref)
        _digest(self.capability_hash, "capability fingerprint")
        _digest(self.package_digest, "package digest")
        _text(self.publisher, "publisher", 256)
        _text(self.version, "implementation version", 128)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "implementation_ref": self.implementation_ref,
            "capability_hash": self.capability_hash,
            "package_digest": self.package_digest,
            "publisher": self.publisher,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, raw: object) -> Self:
        return cls(**_fields(raw, cls._FIELDS))

    @property
    def fingerprint(self) -> str:
        return content_hash(self.to_dict())

    def assert_capability(self, descriptor: ToolCapabilityDescriptor) -> None:
        if not isinstance(descriptor, ToolCapabilityDescriptor):
            raise InvalidInputError("Installed capability contract is required")
        if descriptor.fingerprint != self.capability_hash:
            raise ScopeDeniedError("Implementation is bound to a different capability definition")


@dataclass(frozen=True, slots=True)
class ToolInstallationDescriptor:
    """Workspace-bound *record of configuration*, not an active installation."""

    installation_ref: str
    workspace_id: UUID
    implementation_hash: str
    configuration_hash: str
    policy_revision_hash: str
    permitted_scopes: tuple[str, ...]
    schema_version: int = 1

    _FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "installation_ref", "workspace_id", "implementation_hash",
        "configuration_hash", "policy_revision_hash", "permitted_scopes",
    })

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise InvalidInputError("Unsupported tool installation contract version")
        _instance_ref(self.installation_ref)
        if not isinstance(self.workspace_id, UUID):
            raise InvalidInputError("Workspace identity must be an actual UUID")
        for name in ("implementation_hash", "configuration_hash", "policy_revision_hash"):
            _digest(getattr(self, name), name)
        _scopes(self.permitted_scopes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "installation_ref": self.installation_ref,
            "workspace_id": str(self.workspace_id),
            "implementation_hash": self.implementation_hash,
            "configuration_hash": self.configuration_hash,
            "policy_revision_hash": self.policy_revision_hash,
            "permitted_scopes": list(self.permitted_scopes),
        }

    @classmethod
    def from_dict(cls, raw: object) -> Self:
        value = _fields(raw, cls._FIELDS)
        if not isinstance(value["permitted_scopes"], list):
            raise InvalidInputError("Permission scopes must be a JSON array")
        return cls(
            installation_ref=value["installation_ref"],
            workspace_id=_uuid(value["workspace_id"], "workspace"),
            implementation_hash=value["implementation_hash"],
            configuration_hash=value["configuration_hash"],
            policy_revision_hash=value["policy_revision_hash"],
            permitted_scopes=tuple(value["permitted_scopes"]),
            schema_version=value["schema_version"],
        )

    @property
    def fingerprint(self) -> str:
        return content_hash(self.to_dict())

    def assert_implementation(self, implementation: ToolImplementationDescriptor) -> None:
        if not isinstance(implementation, ToolImplementationDescriptor):
            raise InvalidInputError("Installed implementation contract is required")
        if implementation.fingerprint != self.implementation_hash:
            raise ScopeDeniedError("Tool installation targets a different pinned implementation")


@dataclass(frozen=True, slots=True)
class ToolInvocationIntent:
    """Controller-constructed immutable *intent*; deliberately no approval flag."""

    invocation_id: UUID
    workspace_id: UUID
    task_revision_id: UUID
    installation_hash: str
    input_type_ref: str
    input_hash: str
    effect_class: str
    policy_revision_hash: str
    max_wall_time_ms: int
    max_output_bytes: int
    idempotency_key: str
    schema_version: int = 1

    _FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "invocation_id", "workspace_id", "task_revision_id",
        "installation_hash", "input_type_ref", "input_hash", "effect_class",
        "policy_revision_hash", "max_wall_time_ms", "max_output_bytes", "idempotency_key",
    })

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise InvalidInputError("Unsupported invocation intent version")
        for name in ("invocation_id", "workspace_id", "task_revision_id"):
            if not isinstance(getattr(self, name), UUID):
                raise InvalidInputError(f"{name} must be an actual UUID")
        for name in ("installation_hash", "input_hash", "policy_revision_hash"):
            _digest(getattr(self, name), name)
        _type_ref(self.input_type_ref)
        if self.effect_class not in _EFFECTS:
            raise InvalidInputError("Invalid invocation side-effect class")
        _positive(self.max_wall_time_ms, "tool deadline", 3_600_000)
        _positive(self.max_output_bytes, "tool output limit", MAX_TOOL_OUTPUT_BYTES)
        _text(self.idempotency_key, "idempotency key", 256)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "invocation_id": str(self.invocation_id),
            "workspace_id": str(self.workspace_id),
            "task_revision_id": str(self.task_revision_id),
            "installation_hash": self.installation_hash,
            "input_type_ref": self.input_type_ref,
            "input_hash": self.input_hash,
            "effect_class": self.effect_class,
            "policy_revision_hash": self.policy_revision_hash,
            "max_wall_time_ms": self.max_wall_time_ms,
            "max_output_bytes": self.max_output_bytes,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_dict(cls, raw: object) -> Self:
        value = _fields(raw, cls._FIELDS)
        return cls(
            invocation_id=_uuid(value["invocation_id"], "invocation_id"),
            workspace_id=_uuid(value["workspace_id"], "workspace_id"),
            task_revision_id=_uuid(value["task_revision_id"], "task_revision_id"),
            installation_hash=value["installation_hash"],
            input_type_ref=value["input_type_ref"],
            input_hash=value["input_hash"],
            effect_class=value["effect_class"],
            policy_revision_hash=value["policy_revision_hash"],
            max_wall_time_ms=value["max_wall_time_ms"],
            max_output_bytes=value["max_output_bytes"],
            idempotency_key=value["idempotency_key"],
            schema_version=value["schema_version"],
        )

    @property
    def fingerprint(self) -> str:
        return content_hash(self.to_dict())

    def assert_capability(self, descriptor: ToolCapabilityDescriptor) -> None:
        if not isinstance(descriptor, ToolCapabilityDescriptor):
            raise InvalidInputError("Tool capability contract is required")
        if (
            type_identity(self.input_type_ref) != type_identity(descriptor.input_type_ref)
            or self.effect_class != descriptor.effect_class
        ):
            raise ScopeDeniedError("Tool invocation ports or effects differ from the pinned descriptor")

    def assert_installation(self, installation: ToolInstallationDescriptor) -> None:
        if not isinstance(installation, ToolInstallationDescriptor):
            raise InvalidInputError("Tool installation contract is required")
        if (
            self.installation_hash != installation.fingerprint
            or self.workspace_id != installation.workspace_id
            or self.policy_revision_hash != installation.policy_revision_hash
        ):
            raise ScopeDeniedError("Tool invocation does not match pinned workspace installation")


@dataclass(frozen=True, slots=True)
class ToolObservation:
    """Bounded result metadata, never a declaration of verification."""

    invocation_hash: str
    output_type_ref: str
    status: str
    output_hash: str | None
    artifact_sha256: str | None
    error_code: str | None
    schema_version: int = 1

    _FIELDS: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "invocation_hash", "output_type_ref", "status",
        "output_hash", "artifact_sha256", "error_code",
    })

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise InvalidInputError("Unsupported tool observation version")
        _digest(self.invocation_hash, "invocation fingerprint")
        _type_ref(self.output_type_ref)
        if self.status not in _OUTCOMES:
            raise InvalidInputError("Tool observation has an invalid result status")
        for name in ("output_hash", "artifact_sha256"):
            if getattr(self, name) is not None:
                _digest(getattr(self, name), name)
        if self.status == "succeeded":
            if self.output_hash is None or self.error_code is not None:
                raise InvalidInputError("Successful observations need an output hash, not an error")
        elif self.output_hash is not None or self.artifact_sha256 is not None:
            raise InvalidInputError("Failed/indeterminate operations must not assert completed output")
        if self.error_code is not None:
            _text(self.error_code, "error code", 128)
        if self.status == "indeterminate" and self.error_code is None:
            raise InvalidInputError("Indeterminate observations need a reason code")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "invocation_hash": self.invocation_hash,
            "output_type_ref": self.output_type_ref,
            "status": self.status,
            "output_hash": self.output_hash,
            "artifact_sha256": self.artifact_sha256,
            "error_code": self.error_code,
        }

    @classmethod
    def from_dict(cls, raw: object) -> Self:
        return cls(**_fields(raw, cls._FIELDS))

    @property
    def fingerprint(self) -> str:
        return content_hash(self.to_dict())


def verify_observation_link(
    intent: ToolInvocationIntent, observation: ToolObservation,
    descriptor: ToolCapabilityDescriptor | None = None,
) -> None:
    """Check one hash link; success still does not mean truth or authorization."""
    if not isinstance(intent, ToolInvocationIntent) or not isinstance(observation, ToolObservation):
        raise InvalidInputError("Verified link requires intent and observation contracts")
    if intent.fingerprint != observation.invocation_hash:
        raise ScopeDeniedError("Tool observation refers to a different immutable invocation")
    if descriptor is not None:
        intent.assert_capability(descriptor)
        if type_identity(observation.output_type_ref) != type_identity(descriptor.output_type_ref):
            raise ScopeDeniedError("Tool observation uses an incompatible result Type")
