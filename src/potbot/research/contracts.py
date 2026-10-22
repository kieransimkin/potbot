"""Phase 7 research-action identities, query lineage and immutable observations.

These are admission contracts, not permission to read files, use a network, or
claim that a question has been answered. The controller must bind every need
revision to its own workspace/task and reserve the research budget separately.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools.repository_snapshot import safe_relative_path

_ACTION_TYPE = "synny://type/potbot-research-action"
_OBSERVATION_TYPE = "synny://type/potbot-research-observation"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[^\s\x00-\x1f]{1,160}\Z")


def _string(value: object, name: str, *, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > maximum:
        raise InvalidInputError(f"{name} requires bounded nonempty UTF-8 text")
    if "\x00" in value or any(ord(ch) < 32 and ch not in "\t\n" for ch in value):
        raise InvalidInputError(f"{name} contains unsupported control characters")
    return value


def _digest(value: str, name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise InvalidInputError(f"{name} requires lowercase SHA-256")
    return value


def _parse_uuid(value: object, name: str) -> UUID:
    if not isinstance(value, str):
        raise InvalidInputError(f"{name} requires a canonical UUID")
    try:
        parsed = UUID(value)
        if str(parsed) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise InvalidInputError(f"{name} requires a canonical UUID") from None
    return parsed


def _unique(values: object, name: str, *, maximum: int = 24) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise InvalidInputError(f"{name} must be a distinct bounded tuple")
    for value in values:
        _string(value, name, maximum=256)
    if len(values) != len(set(values)):
        raise InvalidInputError(f"{name} must be a distinct bounded tuple")
    return values


class ResearchCategory(StrEnum):
    REPOSITORY = "repository"
    DOCUMENTATION = "documentation"
    EXAMPLE_CODE = "example_code"
    MCP = "mcp"


class ResearchOperation(StrEnum):
    READ_FILE = "read_file"
    FIND_LITERAL = "find_literal"
    SEARCH_EXAMPLE_CODE = "search_example_code"
    QUERY_SOURCE = "query_source"


class ObservationStatus(StrEnum):
    INSPECTED = "inspected"
    NO_MATCH = "no_match"
    NOT_FOUND = "not_found"
    NON_TEXT = "non_text"
    OVER_LIMIT = "over_limit"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ResearchScope:
    workspace_id: UUID
    task_id: UUID
    contract_revision_id: UUID
    need_id: UUID
    need_revision_id: UUID
    repository_state_id: UUID
    semantic_release_digest: str

    def __post_init__(self) -> None:
        for field in (
            "workspace_id",
            "task_id",
            "contract_revision_id",
            "need_id",
            "need_revision_id",
            "repository_state_id",
        ):
            if not isinstance(getattr(self, field), UUID):
                raise InvalidInputError(f"{field} requires a UUID")
        _digest(self.semantic_release_digest, "semantic_release_digest")

    def to_dict(self) -> dict[str, str]:
        return {
            **{
                k: str(getattr(self, k))
                for k in (
                    "workspace_id",
                    "task_id",
                    "contract_revision_id",
                    "need_id",
                    "need_revision_id",
                    "repository_state_id",
                )
            },
            "semantic_release_digest": self.semantic_release_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> ResearchScope:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise InvalidInputError("research scope fields are invalid")
        return cls(
            workspace_id=_parse_uuid(value["workspace_id"], "workspace_id"),
            task_id=_parse_uuid(value["task_id"], "task_id"),
            contract_revision_id=_parse_uuid(value["contract_revision_id"], "contract_revision_id"),
            need_id=_parse_uuid(value["need_id"], "need_id"),
            need_revision_id=_parse_uuid(value["need_revision_id"], "need_revision_id"),
            repository_state_id=_parse_uuid(value["repository_state_id"], "repository_state_id"),
            semantic_release_digest=_digest(
                value["semantic_release_digest"], "semantic_release_digest"
            ),
        )


@dataclass(frozen=True, slots=True)
class ResearchQuery:
    category: ResearchCategory
    operation: ResearchOperation
    locator: str
    search_text: str
    required_identifiers: tuple[str, ...]
    expected_observation: str
    decision_effect: str
    evidence_requirement: str
    stopping_condition: str
    fallback: str
    target_version: str = "unspecified"
    freshness_seconds: int = 0
    max_results: int = 8
    max_source_bytes: int = 262144

    def __post_init__(self) -> None:
        if not isinstance(self.category, ResearchCategory) or not isinstance(
            self.operation, ResearchOperation
        ):
            raise InvalidInputError("research category and operation must be registered")
        if self.category == ResearchCategory.REPOSITORY:
            # Explicit source locator; generic filesystem paths cannot escape
            # the checked immutable repository snapshot.
            safe_relative_path(self.locator)
            if self.operation == ResearchOperation.QUERY_SOURCE:
                raise InvalidInputError("repository queries must declare an inspect operation")
        else:
            _string(self.locator, "locator", maximum=1024)
            if self.operation != ResearchOperation.QUERY_SOURCE:
                raise InvalidInputError("external sources require a qualified source adapter")
        for name in (
            "search_text",
            "expected_observation",
            "decision_effect",
            "evidence_requirement",
            "stopping_condition",
            "fallback",
            "target_version",
        ):
            _string(getattr(self, name), name)
        _unique(self.required_identifiers, "required_identifiers")
        for identifier in self.required_identifiers:
            if _IDENTIFIER.fullmatch(identifier) is None:
                raise InvalidInputError("required identifiers cannot be split or rewritten")
        if self.operation == ResearchOperation.READ_FILE and self.search_text != "whole_file":
            raise InvalidInputError("read_file uses the explicit whole_file operation")
        if self.operation in {
            ResearchOperation.FIND_LITERAL,
            ResearchOperation.SEARCH_EXAMPLE_CODE,
        }:
            if len(self.search_text) > 160 or "\n" in self.search_text:
                raise InvalidInputError("literal searches must be single-line and bounded")
        for name, lower, upper in (
            ("freshness_seconds", 0, 30 * 86400),
            ("max_results", 1, 100),
            ("max_source_bytes", 1, 262144),
        ):
            item = getattr(self, name)
            if type(item) is not int or not lower <= item <= upper:
                raise InvalidInputError(f"{name} exceeds its declared allowance")

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "operation": self.operation.value,
            "locator": self.locator,
            "search_text": self.search_text,
            "required_identifiers": list(self.required_identifiers),
            "expected_observation": self.expected_observation,
            "decision_effect": self.decision_effect,
            "evidence_requirement": self.evidence_requirement,
            "stopping_condition": self.stopping_condition,
            "fallback": self.fallback,
            "target_version": self.target_version,
            "freshness_seconds": self.freshness_seconds,
            "max_results": self.max_results,
            "max_source_bytes": self.max_source_bytes,
        }

    @classmethod
    def from_dict(cls, value: object) -> ResearchQuery:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise InvalidInputError("research query fields are invalid")
        if not isinstance(value.get("required_identifiers"), list):
            raise InvalidInputError("research identifiers must be a JSON list")
        try:
            return cls(
                **{
                    **value,
                    "category": ResearchCategory(value["category"]),
                    "operation": ResearchOperation(value["operation"]),
                    "required_identifiers": tuple(value["required_identifiers"]),
                }
            )
        except (TypeError, ValueError):
            raise InvalidInputError("invalid research query") from None

    @property
    def normalized_query(self) -> str:
        # Preserve case, punctuation, exact error codes and dependency versions.
        return " ".join(self.search_text.split())


@dataclass(frozen=True, slots=True)
class ResearchAction:
    action_id: UUID
    scope: ResearchScope
    query: ResearchQuery
    predecessor_id: UUID | None = None
    schema_version: int = 1
    primary_type_ref: str = _ACTION_TYPE

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, UUID) or not isinstance(self.scope, ResearchScope):
            raise InvalidInputError("research action requires UUID and scoped identity")
        if not isinstance(self.query, ResearchQuery):
            raise InvalidInputError("research action requires a structured query")
        if self.predecessor_id is not None and not isinstance(self.predecessor_id, UUID):
            raise InvalidInputError("predecessor requires a UUID")
        if self.predecessor_id == self.action_id:
            raise InvalidInputError("research action cannot precede itself")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise InvalidInputError("unknown research schema")
        if self.primary_type_ref != _ACTION_TYPE:
            raise InvalidInputError("research actions require the canonical type ref")

    @property
    def identity(self) -> str:
        # An action identity covers *all* constraints controlling its result:
        # limits, expected evidence, fallback and lineage. Reusing an identical
        # search string with a smaller allowance or a different predecessor
        # must not return an observation made under the wrong contract.
        query = self.query.to_dict()
        query["search_text"] = self.query.normalized_query
        query["required_identifiers"] = sorted(self.query.required_identifiers)
        return content_hash(
            {
                "schema_version": 1,
                "scope": self.scope.to_dict(),
                "query": query,
                "predecessor_id": str(self.predecessor_id) if self.predecessor_id else None,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "primary_type_ref": self.primary_type_ref,
            "action_id": str(self.action_id),
            "scope": self.scope.to_dict(),
            "query": self.query.to_dict(),
            "predecessor_id": str(self.predecessor_id) if self.predecessor_id else None,
            "identity": self.identity,
        }

    @classmethod
    def from_dict(cls, value: object) -> ResearchAction:
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "primary_type_ref",
            "action_id",
            "scope",
            "query",
            "predecessor_id",
            "identity",
        }:
            raise InvalidInputError("research action fields are invalid")
        item = cls(
            action_id=_parse_uuid(value["action_id"], "action_id"),
            scope=ResearchScope.from_dict(value["scope"]),
            query=ResearchQuery.from_dict(value["query"]),
            predecessor_id=(
                _parse_uuid(value["predecessor_id"], "predecessor_id")
                if value["predecessor_id"] is not None
                else None
            ),
            schema_version=value["schema_version"],
            primary_type_ref=value["primary_type_ref"],
        )
        if value["identity"] != item.identity:
            raise InvalidInputError("research action has a mismatched identity")
        return item


class ResearchActionBook:
    """Pure admission and deduplication against controller-supplied need revisions.

    Use a database transaction and command idempotency at the persistence layer;
    this in-memory object is not safe as a durable or cross-worker coordinator.
    """

    def __init__(self, *, workspace_id: UUID, task_id: UUID, need_revisions: dict[UUID, UUID]):
        if not isinstance(workspace_id, UUID) or not isinstance(task_id, UUID):
            raise InvalidInputError("action book requires scope UUIDs")
        if not isinstance(need_revisions, dict) or not all(
            isinstance(k, UUID) and isinstance(v, UUID) for k, v in need_revisions.items()
        ):
            raise InvalidInputError("need revisions require validated UUID references")
        self.workspace_id, self.task_id = workspace_id, task_id
        self.need_revisions = dict(need_revisions)
        self.actions: dict[UUID, ResearchAction] = {}
        self.identities: dict[str, UUID] = {}

    def admit(self, action: ResearchAction) -> UUID:
        if not isinstance(action, ResearchAction):
            raise InvalidInputError("a typed research action is required")
        scope = action.scope
        if (scope.workspace_id, scope.task_id) != (self.workspace_id, self.task_id):
            raise ScopeDeniedError("research action belongs to another workspace or task")
        if self.need_revisions.get(scope.need_id) != scope.need_revision_id:
            raise RevisionConflictError("research action refers to an unknown or stale need")
        if action.predecessor_id is not None:
            previous = self.actions.get(action.predecessor_id)
            if previous is None:
                raise RevisionConflictError("research reformulation predecessor is unavailable")
            if previous.scope != scope or previous.query.category != action.query.category:
                raise RevisionConflictError("research reformulation changed its need or category")
            if previous.query.locator != action.query.locator:
                raise RevisionConflictError("a new source locator requires a new research action")
            if previous.query.target_version != action.query.target_version:
                raise RevisionConflictError("research reformulation changed its target version")
            missing = set(previous.query.required_identifiers) - set(
                action.query.required_identifiers
            )
            if missing or any(
                token not in action.query.search_text for token in action.query.required_identifiers
            ):
                raise RevisionConflictError("research reformulation dropped a required identifier")
            if action.query.search_text == previous.query.search_text:
                raise InvalidInputError("research reformulation did not change its query")
        elif any(
            token not in action.query.search_text for token in action.query.required_identifiers
        ):
            raise InvalidInputError("research query does not retain required identifiers")
        existing = self.identities.get(action.identity)
        if existing is not None:
            return existing
        if action.action_id in self.actions:
            raise RevisionConflictError("research action ID has already been used")
        if len(self.actions) >= 512:
            raise InvalidInputError("research action count exceeds bound")
        self.actions[action.action_id] = action
        self.identities[action.identity] = action.action_id
        return action.action_id


@dataclass(frozen=True, slots=True)
class ResearchObservation:
    action_id: UUID
    status: ObservationStatus
    requested_locator: str
    resolved_locator: str
    source_revision: str
    retrieved_at: datetime
    content_digest: str | None
    excerpt: str | None
    line_start: int | None
    line_end: int | None
    scan_complete: bool
    license_status: str = "unknown"
    schema_version: int = 1
    primary_type_ref: str = _OBSERVATION_TYPE

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, UUID) or not isinstance(self.status, ObservationStatus):
            raise InvalidInputError("observation requires valid identity and status")
        for f in ("requested_locator", "resolved_locator"):
            _string(getattr(self, f), f)
        _digest(self.source_revision, "source_revision")
        if not isinstance(self.retrieved_at, datetime) or self.retrieved_at.tzinfo is None:
            raise InvalidInputError("retrieval time must include timezone")
        if self.content_digest is not None:
            _digest(self.content_digest, "content_digest")
        if self.excerpt is not None:
            _string(self.excerpt, "excerpt", maximum=262144)
            if self.content_digest is None:
                raise InvalidInputError("an inspected excerpt must have a content digest")
        if self.status == ObservationStatus.INSPECTED and (
            self.excerpt is None or self.content_digest is None
        ):
            raise InvalidInputError("inspected observations require content and digest")
        if (self.line_start is None) != (self.line_end is None):
            raise InvalidInputError("line range must include both bounds")
        if self.line_start is not None and (
            self.line_end is None
            or type(self.line_start) is not int
            or type(self.line_end) is not int
            or self.line_start < 1
            or self.line_end < self.line_start
        ):
            raise InvalidInputError("invalid excerpt range")
        if type(self.scan_complete) is not bool or self.license_status != "unknown":
            raise InvalidInputError("source status cannot invent license or completeness")
        if self.schema_version != 1 or self.primary_type_ref != _OBSERVATION_TYPE:
            raise InvalidInputError("observation has the wrong type or schema")

    @property
    def fingerprint(self) -> str:
        return content_hash(
            {
                "type": self.primary_type_ref,
                "action_id": str(self.action_id),
                "status": self.status.value,
                "requested": self.requested_locator,
                "resolved": self.resolved_locator,
                "revision": self.source_revision,
                "time": self.retrieved_at.isoformat(),
                "digest": self.content_digest,
                "excerpt_sha256": (
                    hashlib.sha256(self.excerpt.encode()).hexdigest() if self.excerpt else None
                ),
                "range": [self.line_start, self.line_end],
                "complete": self.scan_complete,
                "license": self.license_status,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "primary_type_ref": self.primary_type_ref,
            "action_id": str(self.action_id),
            "status": self.status.value,
            "requested_locator": self.requested_locator,
            "resolved_locator": self.resolved_locator,
            "source_revision": self.source_revision,
            "retrieved_at": self.retrieved_at.isoformat(),
            "content_digest": self.content_digest,
            "excerpt": self.excerpt,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "scan_complete": self.scan_complete,
            "license_status": self.license_status,
            "fingerprint": self.fingerprint,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_dict()).encode("utf-8")
