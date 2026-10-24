"""Pinned work/provenance contracts. Neither a receipt nor a cursor grants authority."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.syn import SynRef, parse_type_ref

WORK_KINDS = (
    "agent",
    "subagent",
    "workflow",
    "model",
    "mcp_server",
    "mcp_session",
    "mcp_tool",
    "mcp_workflow",
    "tool",
    "cog",
    "browser",
    "api",
    "sql",
    "script",
    "ci",
    "queue",
    "maintenance",
)
TERMINAL = frozenset({"completed", "failed", "cancelled", "indeterminate"})
REQUEST = "syn://type/potbot-work-request-v1"
RESULT = "syn://type/potbot-work-result-v1"
SEAL_REQUEST = "syn://type/potbot-work-seal-request-v1"
SEAL_RESULT = "syn://type/potbot-work-seal-result-v1"
REPLAY_REQUEST = "syn://type/potbot-work-replay-request-v1"
REPLAY_RESULT = "syn://type/potbot-work-replay-result-v1"
RECEIPT = "syn://type/potbot-work-receipt-v1"
EVENT = "syn://type/potbot-work-event-v1"
MAX_INLINE_BYTES = 16384
MAX_TRACE_BYTES = 16 * 1024 * 1024
MAX_EVENTS = 2048
MAX_RUNS = 256


def digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise InvalidInputError("Execution identity requires lowercase SHA-256")
    return value


def identifier(value: object) -> UUID:
    try:
        result = UUID(str(value))
    except (ValueError, AttributeError) as error:
        raise InvalidInputError("Execution reference requires a canonical UUID") from error
    if str(result) != str(value):
        raise InvalidInputError("Execution reference requires a canonical UUID")
    return result


def fields(value: object, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or set(value) - required - (optional or set())
    ):
        raise InvalidInputError("Execution fields differ from the versioned contract")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise InvalidInputError("Unsupported execution contract version")
    if len(canonical_json(value).encode("utf-8")) > MAX_INLINE_BYTES:
        raise InvalidInputError("Execution inline value exceeds its 16 KiB contract")
    return value


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """Host-created policy pin, never constructed from a model-controlled request."""

    policy_revision_hash: str
    configuration_hash: str
    source_digest: str
    executor_version: str
    approval_receipt_hash: str | None = None
    max_wall_ms: int = 30000
    max_output_bytes: int = MAX_INLINE_BYTES
    budget_json: str = "{}"
    host_clock_offset_ms: int | None = None
    host_clock_uncertainty_ms: int | None = None

    def __post_init__(self) -> None:
        for value in (self.policy_revision_hash, self.configuration_hash, self.source_digest):
            digest(value)
        if self.approval_receipt_hash is not None:
            digest(self.approval_receipt_hash)
        if (
            not isinstance(self.executor_version, str)
            or not 1 <= len(self.executor_version) <= 128
            or any(ord(c) < 33 for c in self.executor_version)
        ):
            raise InvalidInputError("Executor revision must be an explicit bounded identifier")
        if type(self.max_wall_ms) is not int or not 1 <= self.max_wall_ms <= 3600000:
            raise InvalidInputError("Execution deadline is outside the bounded policy")
        if (
            type(self.max_output_bytes) is not int
            or not 1 <= self.max_output_bytes <= MAX_INLINE_BYTES
        ):
            raise InvalidInputError("Large output must use a retained artifact reference")
        try:
            if (
                not isinstance(self.budget_json, str)
                or len(self.budget_json.encode("utf-8")) > 4096
            ):
                raise ValueError("Budget exceeds its bounded contract")
            budgets = json.loads(self.budget_json)
            if (
                not isinstance(budgets, dict)
                or len(budgets) > 16
                or canonical_json(budgets) != self.budget_json
            ):
                raise ValueError("Budget is not canonical")
            for name, row in budgets.items():
                if (
                    not isinstance(name, str)
                    or not 1 <= len(name) <= 64
                    or not isinstance(row, dict)
                    or set(row) != {"limit", "unit"}
                ):
                    raise ValueError("Budget needs explicit units")
                if (
                    type(row["limit"]) is not int
                    or row["limit"] < 0
                    or not isinstance(row["unit"], str)
                    or not 1 <= len(row["unit"]) <= 32
                ):
                    raise ValueError("Budget needs bounded nonnegative units")
        except (TypeError, ValueError) as error:
            raise InvalidInputError(
                "Execution budget must be a canonical, unit-bearing contract"
            ) from error
        for estimate in (self.host_clock_offset_ms, self.host_clock_uncertainty_ms):
            if estimate is not None and (
                type(estimate) is not int or not -(86400000) <= estimate <= 86400000
            ):
                raise InvalidInputError("Clock estimates must be bounded integer milliseconds")
        if self.host_clock_uncertainty_ms is not None and self.host_clock_uncertainty_ms < 0:
            raise InvalidInputError("Clock uncertainty cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def fingerprint(self) -> str:
        return content_hash(self.to_dict())


@dataclass(frozen=True, slots=True)
class WorkBinding:
    """Immutable host-selected work identity; input and actor are added by the journal."""

    kind: str
    operation_ref: str
    input_type_ref: str
    output_type_ref: str
    implementation_hash: str
    effect_class: str
    policy: ExecutionPolicy
    parent_event_id: UUID | None = None
    trace_id: str | None = None
    span_id: str | None = None
    links: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in (*WORK_KINDS, "seal", "replay"):
            raise InvalidInputError("Unregistered work kind has no execution exemption")
        if SynRef(self.operation_ref).kind != "thing":
            raise InvalidInputError("Operation identity must be a native thing reference")
        parse_type_ref(self.input_type_ref)
        parse_type_ref(self.output_type_ref)
        digest(self.implementation_hash)
        if self.effect_class not in {
            "read_only",
            "idempotent_write",
            "non_idempotent_write",
            "destructive",
        }:
            raise InvalidInputError("Execution effect class is undeclared")
        if not isinstance(self.policy, ExecutionPolicy):
            raise InvalidInputError("Execution requires an independently supplied policy")
        if (self.trace_id is None) != (self.span_id is None):
            raise InvalidInputError("Trace and span identities must be supplied together")
        if self.parent_event_id is not None and not isinstance(self.parent_event_id, UUID):
            raise InvalidInputError("Direct cause must be an authenticated event reference")
        if (
            not isinstance(self.links, tuple)
            or len(self.links) > 16
            or any(not isinstance(v, UUID) for v in self.links)
            or len(set(self.links)) != len(self.links)
        ):
            raise InvalidInputError("Supporting causes must be distinct bounded event identities")
        for value, length in ((self.trace_id, 32), (self.span_id, 16)):
            if value is not None and (
                not isinstance(value, str)
                or len(value) != length
                or any(c not in "0123456789abcdef" for c in value)
                or set(value) == {"0"}
            ):
                raise InvalidInputError("Trace/span identity is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": self.kind,
            "operation_ref": self.operation_ref,
            "input_type_ref": self.input_type_ref,
            "output_type_ref": self.output_type_ref,
            "implementation_hash": self.implementation_hash,
            "effect_class": self.effect_class,
            "policy": self.policy.to_dict(),
            "parent_event_id": str(self.parent_event_id) if self.parent_event_id else None,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "links": [str(v) for v in self.links],
        }


def remote_target(value: object) -> dict[str, Any]:
    """Validate a declaration only; connecting, resetting or flashing is never implicit."""
    row = fields(
        value,
        {
            "schema_version",
            "protocol",
            "target_ref",
            "probe_ref",
            "core_ref",
            "firmware_sha256",
            "symbols_sha256",
            "halt_revision",
            "endpoint_protected",
            "effects",
            "capabilities",
            "qualification",
        },
    )
    if not isinstance(row["protocol"], str) or row["protocol"] not in {
        "python_remote",
        "gdb_rsp",
        "openocd",
        "pyocd",
        "probe_rs",
        "jtag",
        "swd",
        "icsp",
        "jit_crash_attach",
    }:
        raise InvalidInputError("Unknown remote-debug protocol")
    for key in ("target_ref", "probe_ref", "core_ref"):
        if not isinstance(row[key], str) or not 1 <= len(row[key]) <= 256:
            raise InvalidInputError("Remote target/probe/core identity is required")
    digest(row["firmware_sha256"])
    digest(row["symbols_sha256"])
    if (
        type(row["halt_revision"]) is not int
        or row["halt_revision"] < 1
        or row["endpoint_protected"] is not True
    ):
        raise InvalidInputError(
            "Remote debugging needs a protected endpoint and exact halt revision"
        )
    if row["effects"] != {"reset": False, "flash": False, "erase": False, "unlock": False} or any(
        type(v) is not bool for v in row["effects"].values()
    ):
        raise InvalidInputError("Reset/flash/erase/unlock require a separate admitted operation")
    if (
        not isinstance(row["qualification"], str)
        or row["qualification"] not in {"unqualified", "qualified"}
        or not isinstance(row["capabilities"], dict)
        or set(row["capabilities"]) != {"breakpoints", "step", "stack", "registers", "memory"}
        or any(
            not isinstance(v, str) or v not in {"supported", "unsupported", "unqualified"}
            for v in row["capabilities"].values()
        )
    ):
        raise InvalidInputError("Remote capabilities must report support explicitly")
    return row


def match_remote_target(declared: object, expected: object) -> None:
    """A mismatch invalidates attach/inspection; matching alone grants nothing."""
    if remote_target(declared) != remote_target(expected):
        raise InvalidInputError("Remote target, firmware, symbols or halt revision changed")
