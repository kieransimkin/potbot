"""Strict, provenance-bearing SYN execution occurrences (T01.3 foundation).

A validated envelope is not authorization or proof of a sealed semantic type.
The authenticated controller supplies workspace and emitting principal identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from potbot.domain.errors import InvalidInputError

_TYPE = re.compile(r"(?:syn|synny)://type/[a-z0-9]+(?:[-_][a-z0-9]+)*\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
EXECUTION_TRIGGER_KINDS = frozenset(
    {
        "user_input",
        "api_request",
        "schedule",
        "webhook",
        "parent_event",
        "workflow_step",
        "tool_result",
        "provider_callback",
        "process_output",
        "lifecycle_transition",
    }
)
EXECUTION_STAGES = frozenset(
    {"started", "updated", "completed", "failed", "cancelled", "indeterminate"}
)


@dataclass(frozen=True, slots=True)
class ExecutionEventInput:
    """A causally attributable immutable occurrence, before storage allocation."""

    event_type_ref: str
    event_type_version: str
    subject_type_ref: str
    subject_version: str
    run_id: UUID
    trigger_kind: str
    trigger_ref: str
    trigger_input_sha256: str
    emitter_run_id: UUID
    stage: Literal["started", "updated", "completed", "failed", "cancelled", "indeterminate"]
    parent_event_id: UUID | None = None
    parent_run_id: UUID | None = None
    input_artifact_ref: str | None = None
    output_artifact_ref: str | None = None
    output_sha256: str | None = None
    elapsed_ms: int | None = None

    def __post_init__(self) -> None:
        if any(
            not isinstance(ref, str) or not _TYPE.fullmatch(ref)
            for ref in (self.event_type_ref, self.subject_type_ref)
        ):
            raise InvalidInputError("Execution event requires exact full SYN type references")
        for version in (self.event_type_version, self.subject_version):
            if (
                not isinstance(version, str)
                or not 1 <= len(version) <= 128
                or any(ord(c) < 33 for c in version)
            ):
                raise InvalidInputError("Execution event versions must be explicit bounded values")
        for value in (self.run_id, self.emitter_run_id):
            if not isinstance(value, UUID):
                raise InvalidInputError("Run and emitter identities must be UUIDs")
        if self.parent_run_id is not None and not isinstance(self.parent_run_id, UUID):
            raise InvalidInputError("Parent run must be a UUID")
        if self.parent_event_id is not None and not isinstance(self.parent_event_id, UUID):
            raise InvalidInputError("Parent event must be a UUID")
        if (
            self.trigger_kind not in EXECUTION_TRIGGER_KINDS
            or not isinstance(self.trigger_ref, str)
            or not 1 <= len(self.trigger_ref) <= 512
        ):
            raise InvalidInputError("Execution event must name a bounded direct causal trigger")
        if not isinstance(self.trigger_input_sha256, str) or not _SHA.fullmatch(
            self.trigger_input_sha256
        ):
            raise InvalidInputError("Execution event requires the exact input receipt digest")
        if self.stage not in EXECUTION_STAGES:
            raise InvalidInputError("Unsupported execution event lifecycle stage")
        if (
            self.trigger_kind
            in {
                "parent_event",
                "workflow_step",
                "tool_result",
                "provider_callback",
                "process_output",
                "lifecycle_transition",
            }
            and self.parent_event_id is None
        ):
            raise InvalidInputError("Child event must name its immediate causal parent")
        if self.parent_event_id is not None and self.parent_run_id is None:
            raise InvalidInputError("Causal parent must identify its run")
        if self.output_sha256 is not None and (
            not isinstance(self.output_sha256, str)
            or not _SHA.fullmatch(self.output_sha256)
            or self.output_artifact_ref is None
        ):
            raise InvalidInputError("Output digest requires an output artifact")
        if self.elapsed_ms is not None and (
            type(self.elapsed_ms) is not int or self.elapsed_ms < 0
        ):
            raise InvalidInputError("Elapsed time must be measured nonnegative milliseconds")
        for ref in (self.input_artifact_ref, self.output_artifact_ref):
            if ref is not None and (not isinstance(ref, str) or not 1 <= len(ref) <= 512):
                raise InvalidInputError("Artifact references must be bounded identifiers")
