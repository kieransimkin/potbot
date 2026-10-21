"""Stable application errors, independent of transports and provider SDKs."""

from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    SCOPE_DENIED = "scope_denied"
    REVISION_CONFLICT = "revision_conflict"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INDETERMINATE = "indeterminate"


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
