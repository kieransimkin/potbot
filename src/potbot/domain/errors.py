"""Stable application errors, independent of transports and provider SDKs."""

from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    SCHEMA_VIOLATION = "schema_violation"
    SCOPE_DENIED = "scope_denied"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    REVISION_CONFLICT = "revision_conflict"
    STATE_MISMATCH = "state_mismatch"
    CONTEXT_OVERFLOW = "context_overflow"
    BUDGET_UNAVAILABLE = "budget_unavailable"
    SOURCE_UNAVAILABLE = "source_unavailable"
    MODEL_UNAVAILABLE = "model_unavailable"
    TOOL_FAILURE = "tool_failure"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INDETERMINATE = "indeterminate"
    INTERNAL_ERROR = "internal_error"


class DomainError(Exception):
    """A classified error whose code survives adapters and serialization."""

    code: ErrorCode

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "message": self.message}


class InvalidInputError(DomainError, ValueError):
    code = ErrorCode.INVALID_INPUT


class ScopeDeniedError(DomainError):
    code = ErrorCode.SCOPE_DENIED


class RevisionConflictError(DomainError):
    code = ErrorCode.REVISION_CONFLICT


class OperationTimeoutError(DomainError):
    code = ErrorCode.TIMEOUT


class OperationCancelledError(DomainError):
    code = ErrorCode.CANCELLED


class IndeterminateExecutionError(DomainError):
    code = ErrorCode.INDETERMINATE


class SchemaViolationError(DomainError):
    code = ErrorCode.SCHEMA_VIOLATION


class CapabilityUnavailableError(DomainError):
    code = ErrorCode.CAPABILITY_UNAVAILABLE


class StateMismatchError(DomainError):
    code = ErrorCode.STATE_MISMATCH


class ContextOverflowError(DomainError):
    code = ErrorCode.CONTEXT_OVERFLOW


class BudgetUnavailableError(DomainError):
    code = ErrorCode.BUDGET_UNAVAILABLE


class SourceUnavailableError(DomainError):
    code = ErrorCode.SOURCE_UNAVAILABLE


class ModelUnavailableError(DomainError):
    code = ErrorCode.MODEL_UNAVAILABLE


class ToolFailureError(DomainError):
    code = ErrorCode.TOOL_FAILURE


class InternalError(DomainError):
    code = ErrorCode.INTERNAL_ERROR
