"""Every public error is sanitized and retryability follows operation semantics."""

from uuid import uuid4

import pytest

from potbot.api.tasks import DevelopmentBearerAuth
from potbot.api.wire import OperationSemantics, RequestEnvelope, error_response, event_envelope
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    BudgetUnavailableError,
    CapabilityUnavailableError,
    ContextOverflowError,
    DomainError,
    IndeterminateExecutionError,
    InternalError,
    InvalidInputError,
    ModelUnavailableError,
    OperationCancelledError,
    OperationTimeoutError,
    RevisionConflictError,
    SchemaViolationError,
    ScopeDeniedError,
    SourceUnavailableError,
    StateMismatchError,
    ToolFailureError,
)


@pytest.mark.parametrize(
    "error_type,status,code",
    [
        (InvalidInputError, 400, "invalid_input"),
        (SchemaViolationError, 400, "schema_violation"),
        (ScopeDeniedError, 404, "scope_denied"),
        (CapabilityUnavailableError, 503, "capability_unavailable"),
        (RevisionConflictError, 409, "revision_conflict"),
        (StateMismatchError, 409, "state_mismatch"),
        (ContextOverflowError, 413, "context_overflow"),
        (BudgetUnavailableError, 429, "budget_unavailable"),
        (SourceUnavailableError, 503, "source_unavailable"),
        (ModelUnavailableError, 503, "model_unavailable"),
        (ToolFailureError, 502, "tool_failure"),
        (OperationTimeoutError, 504, "timeout"),
        (OperationCancelledError, 409, "cancelled"),
        (IndeterminateExecutionError, 409, "indeterminate_execution"),
        (InternalError, 500, "internal_error"),
    ],
)
def test_error_taxonomy_is_versioned_and_never_leaks_raw_messages(
    error_type: type[DomainError],
    status: int,
    code: str,
) -> None:
    response = error_response(error_type("secret-other-workspace-record-exists"))
    assert response.status == status
    assert response.body["schema_version"] == 1
    assert response.body["error"]["code"] == code
    assert "secret-other-workspace-record-exists" not in str(response.body)
    assert response.body["error"]["retryable"] is False


def test_unknown_exception_is_not_retried_based_on_its_text() -> None:
    result = error_response(
        RuntimeError("timeout retry this password=secret"),
        semantics=OperationSemantics(read_only=True),
    )
    assert result.body["error"]["code"] == "internal_error"
    assert result.body["error"]["retryable"] is False
    assert "password" not in str(result.body)


def test_timeout_safe_replay_and_uncertain_execution_are_distinct() -> None:
    safe = OperationSemantics(idempotency_key=True)
    assert error_response(OperationTimeoutError("late"), semantics=safe).body["error"]["retryable"]
    assert not error_response(OperationTimeoutError("late")).body["error"]["retryable"]
    assert not error_response(IndeterminateExecutionError("unknown"), semantics=safe).body["error"][
        "retryable"
    ]
    assert not error_response(StateMismatchError("late"), semantics=safe).body["error"]["retryable"]


@pytest.mark.parametrize("version", [0, 2, True, "1", None])
def test_unknown_or_noninteger_wire_version_is_rejected(version: object) -> None:
    with pytest.raises(SchemaViolationError):
        RequestEnvelope.from_dict(
            {
                "schema_version": version,
                "request_id": str(uuid4()),
                "idempotency_key": None,
                "payload": {},
            }
        )


def test_dev_auth_is_explicit_does_not_expose_credentials_and_cannot_use_payload_scope() -> None:
    context = WorkspaceContext(uuid4(), uuid4())
    token = "test-token-" + "a" * 40
    auth = DevelopmentBearerAuth({token: context})
    assert auth(f"Bearer {token}") == context
    assert token not in repr(auth)
    for supplied in (None, token, "Bearer wrong"):
        with pytest.raises(ScopeDeniedError):
            auth(supplied)


def test_event_envelope_rejects_boolean_version_and_preserves_producer() -> None:
    event = {
        "schema_version": 1,
        "id": uuid4(),
        "event_type": "task_submitted",
        "recorded_at": "2026-10-21T00:00:00Z",
        "producer_kind": "human",
        "producer_id": str(uuid4()),
        "details": {"contract_revision_id": str(uuid4())},
    }
    result = event_envelope(event)
    assert result["producer"] == {
        "kind": "human",
        "id": event["producer_id"],
        "deployment_id": None,
    }
    assert result["details"] == event["details"]
    with pytest.raises(SchemaViolationError):
        event_envelope(event | {"schema_version": True})
