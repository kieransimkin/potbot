"""Immutable, explicitly versioned wire records for Phase 1 foundations.

These classes describe observed identities and outcomes. Constructing one never
performs I/O, and setting an outcome does not confer verification authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Self
from uuid import UUID, uuid4

from .errors import ErrorCode, InvalidInputError


def _version(value: int) -> None:
    if type(value) is not int or value != 1:
        raise InvalidInputError("unsupported schema_version; expected 1")


def _uuid(value: object, field: str) -> UUID:
    if not isinstance(value, UUID):
        raise InvalidInputError(f"{field} must be a UUID")
    return value


def _parse_uuid(value: object, field: str) -> UUID:
    if not isinstance(value, str):
        raise InvalidInputError(f"{field} must be a UUID string")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise InvalidInputError(f"{field} is not a UUID") from exc
    if str(parsed) != value:
        raise InvalidInputError(f"{field} must use canonical UUID spelling")
    return parsed


def _object(data: object, fields: set[str]) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != fields:
        raise InvalidInputError("record fields do not match the declared schema")
    _version(data["schema_version"])
    return data


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"{field} must be a nonempty string")
    return value


def _aware(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidInputError(f"{field} must be a timezone-aware datetime")
    return value


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise InvalidInputError(f"{field} must be an ISO timestamp string")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidInputError(f"{field} is not an ISO timestamp") from exc
    return _aware(result, field)


class JsonRecord:
    """The JSON contract uses exact keys and rejects unsupported versions."""

    __slots__ = ()

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def from_dict(cls, data: object) -> Self:
        raise NotImplementedError

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> Self:
        try:
            data = json.loads(value)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidInputError("invalid JSON record") from exc
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class RecordId(JsonRecord):
    value: UUID
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _uuid(self.value, "value")

    @classmethod
    def new(cls) -> Self:
        return cls(uuid4())

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "value": str(self.value)}

    @classmethod
    def from_dict(cls, data: object) -> Self:
        value = _object(data, {"schema_version", "value"})
        return cls(_parse_uuid(value["value"], "value"), value["schema_version"])


@dataclass(frozen=True, slots=True)
class ContentDigest(JsonRecord):
    value: str
    algorithm: str = "sha256"
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if self.algorithm != "sha256":
            raise InvalidInputError("unsupported digest algorithm; expected sha256")
        if not isinstance(self.value, str) or re.fullmatch(r"[0-9a-f]{64}", self.value) is None:
            raise InvalidInputError("sha256 value must be 64 lowercase hexadecimal characters")

    @classmethod
    def from_bytes(cls, value: bytes) -> Self:
        if not isinstance(value, bytes):
            raise InvalidInputError("digest input must be bytes")
        return cls(hashlib.sha256(value).hexdigest())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "algorithm": self.algorithm,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        value = _object(data, {"schema_version", "algorithm", "value"})
        return cls(value["value"], value["algorithm"], value["schema_version"])


class ProducerKind(StrEnum):
    MODEL = "model"
    CONTROLLER = "controller"
    TOOL = "tool"
    HUMAN = "human"


@dataclass(frozen=True, slots=True)
class ProducerIdentity(JsonRecord):
    producer_id: UUID
    kind: ProducerKind
    name: str
    deployment_id: UUID | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _uuid(self.producer_id, "producer_id")
        if not isinstance(self.kind, ProducerKind):
            raise InvalidInputError("kind must be a ProducerKind")
        _nonempty(self.name, "name")
        if self.kind is ProducerKind.MODEL:
            _uuid(self.deployment_id, "deployment_id")
        elif self.deployment_id is not None:
            raise InvalidInputError("only model producers may have a deployment_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "producer_id": str(self.producer_id),
            "kind": self.kind.value,
            "name": self.name,
            "deployment_id": str(self.deployment_id) if self.deployment_id else None,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        value = _object(data, {"schema_version", "producer_id", "kind", "name", "deployment_id"})
        try:
            kind = ProducerKind(value["kind"])
        except (TypeError, ValueError) as exc:
            raise InvalidInputError("unknown producer kind") from exc
        return cls(
            _parse_uuid(value["producer_id"], "producer_id"),
            kind,
            value["name"],
            _parse_uuid(value["deployment_id"], "deployment_id")
            if value["deployment_id"] is not None
            else None,
            value["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class EnvironmentRef(JsonRecord):
    environment_id: UUID
    manifest_digest: ContentDigest
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _uuid(self.environment_id, "environment_id")
        if not isinstance(self.manifest_digest, ContentDigest):
            raise InvalidInputError("manifest_digest must be a ContentDigest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "environment_id": str(self.environment_id),
            "manifest_digest": self.manifest_digest.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        value = _object(data, {"schema_version", "environment_id", "manifest_digest"})
        return cls(
            _parse_uuid(value["environment_id"], "environment_id"),
            ContentDigest.from_dict(value["manifest_digest"]),
            value["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class ErrorInfo(JsonRecord):
    code: ErrorCode
    message: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if not isinstance(self.code, ErrorCode):
            raise InvalidInputError("code must be an ErrorCode")
        _nonempty(self.message, "message")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "code": self.code.value,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        value = _object(data, {"schema_version", "code", "message"})
        try:
            code = ErrorCode(value["code"])
        except (TypeError, ValueError) as exc:
            raise InvalidInputError("unknown error code") from exc
        return cls(code, value["message"], value["schema_version"])


class OutcomeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class OperationOutcome(JsonRecord):
    operation_id: UUID
    status: OutcomeStatus
    producer: ProducerIdentity
    environment: EnvironmentRef
    started_at: datetime
    observed_at: datetime
    finished_at: datetime | None = None
    result_digest: ContentDigest | None = None
    error: ErrorInfo | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _uuid(self.operation_id, "operation_id")
        if not isinstance(self.status, OutcomeStatus):
            raise InvalidInputError("status must be an OutcomeStatus")
        if not isinstance(self.producer, ProducerIdentity) or not isinstance(
            self.environment, EnvironmentRef
        ):
            raise InvalidInputError("outcome requires typed producer and environment references")
        _aware(self.started_at, "started_at")
        _aware(self.observed_at, "observed_at")
        if self.observed_at < self.started_at:
            raise InvalidInputError("observed_at precedes started_at")
        if self.finished_at is not None:
            _aware(self.finished_at, "finished_at")
            if not self.started_at <= self.finished_at <= self.observed_at:
                raise InvalidInputError("finished_at must lie between start and observation")
        if self.result_digest is not None and not isinstance(self.result_digest, ContentDigest):
            raise InvalidInputError("result_digest must be a ContentDigest")
        if self.error is not None and not isinstance(self.error, ErrorInfo):
            raise InvalidInputError("error must be ErrorInfo")
        if self.status is OutcomeStatus.SUCCEEDED:
            if self.error is not None or self.result_digest is None or self.finished_at is None:
                raise InvalidInputError(
                    "success requires a result and finish time without an error"
                )
        else:
            if self.error is None or self.result_digest is not None:
                raise InvalidInputError(
                    "non-success requires an error and cannot publish a success result"
                )
            required = {
                OutcomeStatus.CANCELLED: ErrorCode.CANCELLED,
                OutcomeStatus.TIMED_OUT: ErrorCode.TIMEOUT,
                OutcomeStatus.INDETERMINATE: ErrorCode.INDETERMINATE,
            }.get(self.status)
            if required is not None and self.error.code is not required:
                raise InvalidInputError("outcome status and error code disagree")
            if self.status is OutcomeStatus.FAILED and self.error.code in {
                ErrorCode.CANCELLED,
                ErrorCode.TIMEOUT,
                ErrorCode.INDETERMINATE,
            }:
                raise InvalidInputError(
                    "control and uncertain outcomes cannot be classified as failed"
                )
            if self.status is OutcomeStatus.INDETERMINATE:
                if self.finished_at is not None:
                    raise InvalidInputError("indeterminate execution cannot assert a finish time")
            elif self.finished_at is None:
                raise InvalidInputError("a confirmed terminal outcome requires a finish time")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operation_id": str(self.operation_id),
            "status": self.status.value,
            "producer": self.producer.to_dict(),
            "environment": self.environment.to_dict(),
            "started_at": self.started_at.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at is not None else None,
            "result_digest": self.result_digest.to_dict()
            if self.result_digest is not None
            else None,
            "error": self.error.to_dict() if self.error is not None else None,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        value = _object(
            data,
            {
                "schema_version",
                "operation_id",
                "status",
                "producer",
                "environment",
                "started_at",
                "observed_at",
                "finished_at",
                "result_digest",
                "error",
            },
        )
        try:
            status = OutcomeStatus(value["status"])
        except (TypeError, ValueError) as exc:
            raise InvalidInputError("unknown outcome status") from exc
        return cls(
            operation_id=_parse_uuid(value["operation_id"], "operation_id"),
            status=status,
            producer=ProducerIdentity.from_dict(value["producer"]),
            environment=EnvironmentRef.from_dict(value["environment"]),
            started_at=_parse_time(value["started_at"], "started_at"),
            observed_at=_parse_time(value["observed_at"], "observed_at"),
            finished_at=_parse_time(value["finished_at"], "finished_at")
            if value["finished_at"] is not None
            else None,
            result_digest=ContentDigest.from_dict(value["result_digest"])
            if value["result_digest"] is not None
            else None,
            error=ErrorInfo.from_dict(value["error"]) if value["error"] is not None else None,
            schema_version=value["schema_version"],
        )
