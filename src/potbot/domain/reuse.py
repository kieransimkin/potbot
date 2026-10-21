"""Immutable records for inspecting reuse opportunities before coding.

These records validate supplied evidence, not whether a search really ran or a
source is trustworthy. Tool receipts and source verification belong to future
controller adapters. Every search remains bound to its task, project, workspace,
query, and declared result limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from .errors import InvalidInputError
from .records import JsonRecord, _nonempty, _object, _parse_uuid, _uuid, _version


def _enum[Enum: StrEnum](kind: type[Enum], value: object) -> Enum:
    if not isinstance(value, str):
        raise InvalidInputError(f"unknown {kind.__name__}")
    try:
        return kind(value)
    except (TypeError, ValueError) as exc:
        raise InvalidInputError(f"unknown {kind.__name__}") from exc


def _records[Record: JsonRecord](
    value: object, kind: type[Record], field: str
) -> tuple[Record, ...]:
    if not isinstance(value, list):
        raise InvalidInputError(f"{field} must be a JSON array")
    return tuple(kind.from_dict(item) for item in value)


def _typed_tuple(value: object, kind: type[Any], field: str) -> None:
    if not isinstance(value, tuple) or not all(isinstance(item, kind) for item in value):
        raise InvalidInputError(f"{field} must be an immutable tuple of {kind.__name__} values")


def _rationale(value: object, field: str) -> None:
    if len(_nonempty(value, field)) > 1000:
        raise InvalidInputError(f"{field} must be concise (at most 1000 characters)")


class SearchChannel(StrEnum):
    EXTERNAL_LIBRARIES = "external_libraries"
    EXTERNAL_TOOLS = "external_tools"
    INTERNAL_WORKFLOWS = "internal_workflows"


class SearchStatus(StrEnum):
    COMPLETED = "completed"
    UNAVAILABLE = "unavailable"
    BUDGET_LIMITED = "budget_limited"


class ReuseAction(StrEnum):
    REUSE = "reuse"
    ADAPT = "adapt"
    BUILD = "build"


class ReviewStatus(StrEnum):
    STRUCTURALLY_READY = "structurally_ready"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class ReuseScope(JsonRecord):
    task_id: UUID
    project_id: UUID
    workspace_id: UUID
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        for field in ("task_id", "project_id", "workspace_id"):
            _uuid(getattr(self, field), field)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": str(self.task_id),
            "project_id": str(self.project_id),
            "workspace_id": str(self.workspace_id),
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        value = _object(data, {"schema_version", "task_id", "project_id", "workspace_id"})
        return cls(
            _parse_uuid(value["task_id"], "task_id"),
            _parse_uuid(value["project_id"], "project_id"),
            _parse_uuid(value["workspace_id"], "workspace_id"),
            value["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class SearchIntent(JsonRecord):
    scope: ReuseScope
    channel: SearchChannel
    query: str
    max_results: int = 5
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if not isinstance(self.scope, ReuseScope) or not isinstance(self.channel, SearchChannel):
            raise InvalidInputError("search intent requires a typed scope and channel")
        _nonempty(self.query, "query")
        if type(self.max_results) is not int or not 1 <= self.max_results <= 20:
            raise InvalidInputError("max_results must be an integer between 1 and 20")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scope": self.scope.to_dict(),
            "channel": self.channel.value,
            "query": self.query,
            "max_results": self.max_results,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        value = _object(data, {"schema_version", "scope", "channel", "query", "max_results"})
        return cls(
            ReuseScope.from_dict(value["scope"]),
            _enum(SearchChannel, value["channel"]),
            value["query"],
            value["max_results"],
            value["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class ReusePlan(JsonRecord):
    scope: ReuseScope
    task_summary: str
    intents: tuple[SearchIntent, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if not isinstance(self.scope, ReuseScope):
            raise InvalidInputError("plan requires a ReuseScope")
        _nonempty(self.task_summary, "task_summary")
        _typed_tuple(self.intents, SearchIntent, "intents")
        if len(self.intents) != len(SearchChannel) or {
            intent.channel for intent in self.intents
        } != set(SearchChannel):
            raise InvalidInputError("plan requires exactly one intent for each search channel")
        if any(intent.scope != self.scope for intent in self.intents):
            raise InvalidInputError("search intent task or scope differs from its plan")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scope": self.scope.to_dict(),
            "task_summary": self.task_summary,
            "intents": [intent.to_dict() for intent in self.intents],
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        value = _object(data, {"schema_version", "scope", "task_summary", "intents"})
        return cls(
            ReuseScope.from_dict(value["scope"]),
            value["task_summary"],
            _records(value["intents"], SearchIntent, "intents"),
            value["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class ReuseCandidate(JsonRecord):
    candidate_id: UUID
    name: str
    source_ref: str
    summary: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _uuid(self.candidate_id, "candidate_id")
        for field in ("name", "source_ref", "summary"):
            _nonempty(getattr(self, field), field)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": str(self.candidate_id),
            "name": self.name,
            "source_ref": self.source_ref,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        value = _object(data, {"schema_version", "candidate_id", "name", "source_ref", "summary"})
        return cls(
            _parse_uuid(value["candidate_id"], "candidate_id"),
            value["name"],
            value["source_ref"],
            value["summary"],
            value["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class SearchEvidence(JsonRecord):
    """A caller-supplied attempt record, including explicit search limitations.

    evidence_ref identifies the attempt's record; candidate source_ref identifies
    supporting material. Neither string's existence or authority is verified here.
    A budget-limited attempt may retain useful partial results.
    """

    intent: SearchIntent
    status: SearchStatus
    evidence_ref: str
    candidates: tuple[ReuseCandidate, ...] = ()
    reason: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if not isinstance(self.intent, SearchIntent) or not isinstance(self.status, SearchStatus):
            raise InvalidInputError("search evidence requires a typed intent and status")
        _nonempty(self.evidence_ref, "evidence_ref")
        _typed_tuple(self.candidates, ReuseCandidate, "candidates")
        if len(self.candidates) > self.intent.max_results:
            raise InvalidInputError("candidate count exceeds the planned result limit")
        if len({candidate.candidate_id for candidate in self.candidates}) != len(self.candidates):
            raise InvalidInputError("one search cannot repeat a candidate identifier")
        if self.status is SearchStatus.COMPLETED:
            if self.reason is not None:
                raise InvalidInputError("completed search cannot carry a limitation reason")
        else:
            _rationale(self.reason, "reason")
            if self.status is SearchStatus.UNAVAILABLE and self.candidates:
                raise InvalidInputError("unavailable search cannot claim discovered candidates")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "intent": self.intent.to_dict(),
            "status": self.status.value,
            "evidence_ref": self.evidence_ref,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        value = _object(
            data, {"schema_version", "intent", "status", "evidence_ref", "candidates", "reason"}
        )
        return cls(
            SearchIntent.from_dict(value["intent"]),
            _enum(SearchStatus, value["status"]),
            value["evidence_ref"],
            _records(value["candidates"], ReuseCandidate, "candidates"),
            value["reason"],
            value["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class ReuseDecision(JsonRecord):
    """References selected candidates for reuse/adapt or considered ones for build."""

    scope: ReuseScope
    action: ReuseAction
    rationale: str
    candidate_ids: tuple[UUID, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if not isinstance(self.scope, ReuseScope) or not isinstance(self.action, ReuseAction):
            raise InvalidInputError("decision requires a typed scope and action")
        _rationale(self.rationale, "rationale")
        _typed_tuple(self.candidate_ids, UUID, "candidate_ids")
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise InvalidInputError("decision cannot repeat a candidate identifier")
        if self.action in {ReuseAction.REUSE, ReuseAction.ADAPT} and not self.candidate_ids:
            raise InvalidInputError("reuse or adapt requires at least one candidate reference")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scope": self.scope.to_dict(),
            "action": self.action.value,
            "rationale": self.rationale,
            "candidate_ids": [str(candidate_id) for candidate_id in self.candidate_ids],
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        value = _object(data, {"schema_version", "scope", "action", "rationale", "candidate_ids"})
        if not isinstance(value["candidate_ids"], list):
            raise InvalidInputError("candidate_ids must be a JSON array")
        return cls(
            ReuseScope.from_dict(value["scope"]),
            _enum(ReuseAction, value["action"]),
            value["rationale"],
            tuple(_parse_uuid(item, "candidate_id") for item in value["candidate_ids"]),
            value["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class ReuseAssessment(JsonRecord):
    """A complete, consistent review; it is not authorization or verified research."""

    plan: ReusePlan
    searches: tuple[SearchEvidence, ...]
    decision: ReuseDecision
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if not isinstance(self.plan, ReusePlan) or not isinstance(self.decision, ReuseDecision):
            raise InvalidInputError("assessment requires a typed plan and explicit decision")
        _typed_tuple(self.searches, SearchEvidence, "searches")
        if self.decision.scope != self.plan.scope:
            raise InvalidInputError("decision task or scope differs from its plan")
        planned = {intent.channel: intent for intent in self.plan.intents}
        covered: set[SearchChannel] = set()
        candidates: dict[UUID, ReuseCandidate] = {}
        for search in self.searches:
            if search.intent != planned[search.intent.channel]:
                raise InvalidInputError(
                    "search evidence task, scope, query, or budget differs from plan"
                )
            if search.intent.channel in covered:
                raise InvalidInputError("assessment cannot repeat a search channel")
            covered.add(search.intent.channel)
            for candidate in search.candidates:
                previous = candidates.get(candidate.candidate_id)
                if previous is not None and previous != candidate:
                    raise InvalidInputError("candidate identifier refers to conflicting evidence")
                candidates[candidate.candidate_id] = candidate
        missing = set(SearchChannel) - covered
        if missing:
            names = ", ".join(sorted(channel.value for channel in missing))
            raise InvalidInputError(f"reuse review incomplete; missing search channels: {names}")
        if any(candidate_id not in candidates for candidate_id in self.decision.candidate_ids):
            raise InvalidInputError("decision references a candidate absent from search evidence")

    @property
    def degraded_channels(self) -> tuple[SearchChannel, ...]:
        limited = {
            search.intent.channel
            for search in self.searches
            if search.status is not SearchStatus.COMPLETED
        }
        return tuple(intent.channel for intent in self.plan.intents if intent.channel in limited)

    @property
    def status(self) -> ReviewStatus:
        return ReviewStatus.DEGRADED if self.degraded_channels else ReviewStatus.STRUCTURALLY_READY

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan": self.plan.to_dict(),
            "searches": [search.to_dict() for search in self.searches],
            "decision": self.decision.to_dict(),
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        value = _object(data, {"schema_version", "plan", "searches", "decision", "status"})
        assessment = cls(
            ReusePlan.from_dict(value["plan"]),
            _records(value["searches"], SearchEvidence, "searches"),
            ReuseDecision.from_dict(value["decision"]),
            value["schema_version"],
        )
        if _enum(ReviewStatus, value["status"]) is not assessment.status:
            raise InvalidInputError("review status contradicts its search evidence")
        return assessment
