"""Bounded job contracts shared by workers, REST and WebSocket adapters."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json

MAX_JOB_INPUT_BYTES = 1024 * 1024
MAX_JOB_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_DELTA_BYTES = 64 * 1024
MAX_SAFE_INTEGER = 2**53 - 1
_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,95}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:@+\-]{0,249}\Z")


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INDETERMINATE = "indeterminate"


TERMINAL_JOB_STATES = frozenset(
    {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.INDETERMINATE}
)
ACTIVE_JOB_STATES = frozenset({JobState.RUNNING, JobState.CANCELLING})
JOB_TYPES = frozenset({"model.generate"})


def identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        raise InvalidInputError(f"{name} requires a bounded identifier.")
    return value


def safe_integer(value: object, name: str, *, maximum: int = MAX_SAFE_INTEGER) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise InvalidInputError(f"{name} requires a nonnegative bounded integer.")
    return value


def lease_duration(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0 < value <= 3600
    ):
        raise InvalidInputError("Lease duration must be finite and between 0 and 3600 seconds.")
    return float(value)


def _json_object(value: object, *, maximum: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidInputError("The job value must be a bounded JSON object.")
    try:
        encoded = canonical_json(value)
        if len(encoded.encode("utf-8")) > maximum:
            raise InvalidInputError("The job value exceeds its byte allowance.")
        copied: dict[str, Any] = json.loads(encoded)
        return copied
    except UnicodeError:
        raise InvalidInputError("The job value requires valid Unicode.") from None


def selected_models(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise InvalidInputError("Select between one and sixteen model identities.")
    result = []
    for selection in value:
        if not isinstance(selection, dict) or set(selection) != {"provider", "model", "role"}:
            raise InvalidInputError("A model selection requires provider, model and role.")
        provider = identifier(selection["provider"], "Provider")
        model = selection["model"]
        if not isinstance(model, str) or not _MODEL.fullmatch(model):
            raise InvalidInputError("Model identity must be a bounded model identifier.")
        role = selection["role"]
        if not isinstance(role, str) or role not in {"reasoning", "coding"}:
            raise InvalidInputError("A selected model must have a reasoning or coding role.")
        item = {"provider": provider, "model": model, "role": role}
        if item in result:
            raise InvalidInputError("Model selections must be distinct.")
        result.append(item)
    return result


@dataclass(frozen=True, slots=True)
class JobSpec:
    job_type: str
    episode_id: UUID
    input: dict[str, Any] = field(repr=False)
    selected_models: list[dict[str, str]]

    @classmethod
    def from_dict(cls, value: object) -> JobSpec:
        if not isinstance(value, dict) or set(value) != {
            "job_type",
            "episode_id",
            "input",
            "selected_models",
        }:
            raise InvalidInputError("Job fields do not match the supported schema.")
        if not isinstance(value["job_type"], str) or value["job_type"] not in JOB_TYPES:
            raise InvalidInputError("This job type has no installed handler.")
        try:
            raw_id = value["episode_id"]
            if not isinstance(raw_id, str):
                raise ValueError
            episode_id = UUID(raw_id)
            if str(episode_id) != raw_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise InvalidInputError("The job requires a canonical task identity.") from None
        return cls(
            value["job_type"],
            episode_id,
            _json_object(value["input"], maximum=MAX_JOB_INPUT_BYTES),
            selected_models(value["selected_models"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_type": self.job_type,
            "episode_id": str(self.episode_id),
            "input": _json_object(self.input, maximum=MAX_JOB_INPUT_BYTES),
            "selected_models": selected_models(self.selected_models),
        }


@dataclass(frozen=True, slots=True)
class JobLease:
    workspace_id: UUID
    job_id: UUID
    generation: int
    worker_id: str
    lease_until: datetime
    job_type: str
    episode_id: UUID
    input: dict[str, Any] = field(repr=False)
    selected_models: list[dict[str, str]]
    contract_revision_id: UUID


def progress_data(stage: object, completed: object = None, total: object = None) -> dict[str, Any]:
    result: dict[str, Any] = {"stage": identifier(stage, "Progress stage")}
    if (completed is None) != (total is None):
        raise InvalidInputError("Progress counts require both completed and total.")
    if completed is not None:
        completed_count = safe_integer(completed, "Completed progress")
        total_count = safe_integer(total, "Total progress")
        if completed_count > total_count:
            raise InvalidInputError("Completed progress exceeds the total.")
        result.update(completed=completed_count, total=total_count)
    return result


def result_metadata(value: object) -> dict[str, Any]:
    result = _json_object(value, maximum=16384)
    allowed = {
        "artifact_id",
        "invocation_id",
        "invocation_ids",
        "operation_id",
        "usage",
        "provider",
        "model",
        "finish_reason",
        "accounting",
        "completed_models",
    }
    if not set(result).issubset(allowed):
        raise InvalidInputError("Job results accept only declared public metadata.")
    for name in {"artifact_id", "invocation_id", "operation_id"} & result.keys():
        try:
            if not isinstance(result[name], str) or str(UUID(result[name])) != result[name]:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise InvalidInputError("Result references must be canonical UUIDs.") from None
    for name in {"provider", "finish_reason"} & result.keys():
        identifier(result[name], name)
    if "invocation_ids" in result:
        values = result["invocation_ids"]
        if not isinstance(values, list) or len(values) > 16:
            raise InvalidInputError("Invocation references must be a bounded list.")
        for value in values:
            try:
                if not isinstance(value, str) or str(UUID(value)) != value:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise InvalidInputError("Invocation references must be canonical UUIDs.") from None
        if len(set(values)) != len(values):
            raise InvalidInputError("Invocation references must be distinct.")
    if "accounting" in result and (
        not isinstance(result["accounting"], str)
        or result["accounting"]
        not in {
            "conservative_estimate",
            "provider_reported",
            "measured",
            "mixed",
            "pending_reconciliation",
        }
    ):
        raise InvalidInputError("Accounting origin must identify its evidence.")
    if "completed_models" in result:
        safe_integer(result["completed_models"], "Completed models", maximum=16)
    if "model" in result and (
        not isinstance(result["model"], str) or not _MODEL.fullmatch(result["model"])
    ):
        raise InvalidInputError("Result model must be a bounded model identifier.")
    if "usage" in result:
        usage = result["usage"]
        if not isinstance(usage, dict) or not set(usage).issubset(
            {"input_tokens", "output_tokens", "tokens", "calls", "inference_ms", "money_microunits"}
        ):
            raise InvalidInputError("Result usage fields do not match the accounting schema.")
        for amount in usage.values():
            safe_integer(amount, "Usage")
    return result
