"""Versioned public envelopes and error mapping without exception-text heuristics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from potbot.domain.errors import DomainError, ErrorCode, SchemaViolationError


@dataclass(frozen=True, slots=True)
class RequestEnvelope:
    request_id: UUID
    payload: dict[str, Any]
    idempotency_key: str | None
    schema_version: int = 1

    @classmethod
    def from_dict(cls, value: object) -> RequestEnvelope:
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "request_id",
            "payload",
            "idempotency_key",
        }:
            raise SchemaViolationError("request fields do not match v1 envelope")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise SchemaViolationError("unsupported API schema_version")
        if not isinstance(value["payload"], dict):
            raise SchemaViolationError("request payload must be an object")
        key = value["idempotency_key"]
        if key is not None and (not isinstance(key, str) or not key.strip() or len(key) > 200):
            raise SchemaViolationError("idempotency_key must be nonempty and bounded")
        try:
            if not isinstance(value["request_id"], str):
                raise ValueError
            identifier = UUID(value["request_id"])
            if str(identifier) != value["request_id"]:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise SchemaViolationError("request_id must be a canonical UUID") from None
        return cls(identifier, value["payload"].copy(), key)


@dataclass(frozen=True, slots=True)
class OperationSemantics:
    read_only: bool = False
    idempotency_key: bool = False
    outcome_known: bool = False


_PUBLIC_ERRORS: dict[ErrorCode, tuple[int, str]] = {
    ErrorCode.INVALID_INPUT: (400, "The request is invalid."),
    ErrorCode.SCHEMA_VIOLATION: (400, "The request does not match the supported versioned schema."),
    ErrorCode.SCOPE_DENIED: (404, "The requested resource is unavailable."),
    ErrorCode.CAPABILITY_UNAVAILABLE: (503, "The required capability is unavailable."),
    ErrorCode.REVISION_CONFLICT: (409, "The record changed; inspect its current revision."),
    ErrorCode.STATE_MISMATCH: (409, "The operation is not allowed in the current state."),
    ErrorCode.CONTEXT_OVERFLOW: (413, "The input exceeds the configured context limit."),
    ErrorCode.BUDGET_UNAVAILABLE: (429, "The task resource allowance is unavailable."),
    ErrorCode.SOURCE_UNAVAILABLE: (503, "The requested source is unavailable."),
    ErrorCode.MODEL_UNAVAILABLE: (503, "The configured model is unavailable."),
    ErrorCode.TOOL_FAILURE: (502, "The tool reported a failure."),
    ErrorCode.TIMEOUT: (504, "The operation exceeded its deadline."),
    ErrorCode.CANCELLED: (409, "The operation was cancelled."),
    ErrorCode.INDETERMINATE: (409, "The operation outcome is uncertain and needs reconciliation."),
    ErrorCode.INTERNAL_ERROR: (500, "The operation could not be completed."),
}


@dataclass(frozen=True, slots=True)
class APIResponse:
    status: int
    body: dict[str, Any]


_DEFAULT_SEMANTICS = OperationSemantics()


def error_response(
    error: Exception,
    *,
    request_id: UUID | None = None,
    semantics: OperationSemantics = _DEFAULT_SEMANTICS,
) -> APIResponse:
    """Never expose raw database/provider messages or infer retryability from text."""
    code = error.code if isinstance(error, DomainError) else ErrorCode.INTERNAL_ERROR
    status, message = _PUBLIC_ERRORS[code]
    safe_repeat = semantics.read_only or semantics.idempotency_key
    transient = code in {ErrorCode.SOURCE_UNAVAILABLE, ErrorCode.MODEL_UNAVAILABLE}
    retryable = safe_repeat and (
        code is ErrorCode.TIMEOUT or (transient and semantics.outcome_known)
    )
    wire_code = "indeterminate_execution" if code is ErrorCode.INDETERMINATE else code.value
    return APIResponse(
        status,
        {
            "schema_version": 1,
            "request_id": str(request_id) if request_id else None,
            "data": None,
            "error": {"code": wire_code, "message": message, "retryable": retryable},
        },
    )


def success_response(request_id: UUID, data: object, *, status: int = 200) -> APIResponse:
    return APIResponse(
        status,
        {
            "schema_version": 1,
            "request_id": str(request_id),
            "data": data,
            "error": None,
        },
    )


def event_envelope(event: dict[str, Any]) -> dict[str, Any]:
    """Declare event wire version independently of its producer's payload schema."""
    from potbot.controller.tasks import json_record

    version = event.get("schema_version", 1)
    if type(version) is not int or version != 1:
        raise SchemaViolationError("unsupported event schema_version")
    if not {"id", "event_type", "recorded_at"}.issubset(event):
        raise SchemaViolationError("event identity is incomplete")
    return {
        "schema_version": 1,
        "event_id": str(event["id"]),
        "episode_id": str(event["episode_id"]) if event.get("episode_id") else None,
        "event_type": event["event_type"],
        "occurred_at": json_record(event.get("occurred_at")),
        "recorded_at": json_record(event["recorded_at"]),
        "causation_id": str(event["causation_id"]) if event.get("causation_id") else None,
        "correlation_id": str(event["correlation_id"]) if event.get("correlation_id") else None,
        "payload_ref": str(event["payload_id"]) if event.get("payload_id") else None,
        "content_digest": event.get("content_digest"),
        "producer": {
            "kind": event.get("producer_kind"),
            "id": event.get("producer_id"),
            "deployment_id": str(event["deployment_id"]) if event.get("deployment_id") else None,
        },
        "details": json_record(event.get("details", {})),
    }
