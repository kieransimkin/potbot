"""Bounded deterministic depth-first frontier with conservative uncertain outcomes.

This is a *policy projection*, not a provider or tool executor. The production
controller must persist claims and invoke the existing lease/budget gateway before
performing any external effect. Snapshots are inspectable and replayable; they
never authorize execution merely because they can be deserialized.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from potbot.domain.errors import InvalidInputError, RevisionConflictError, StateMismatchError
from potbot.domain.search_graph import SolvePolicy
from potbot.domain.serialization import content_hash


class SearchStatus(StrEnum):
    READY = "ready"
    CLAIMED = "claimed"
    FAILED = "failed"
    VERIFIED = "verified"
    BLOCKED = "blocked"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class FrontierItem:
    node_id: UUID
    parent_id: UUID | None
    depth: int
    state_digest: str
    producer: str
    status: SearchStatus = SearchStatus.READY
    generation: int = 0
    result_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.node_id, UUID) or (
            self.parent_id is not None and not isinstance(self.parent_id, UUID)
        ):
            raise InvalidInputError("Frontier items require UUID node references")
        if self.parent_id == self.node_id:
            raise InvalidInputError("A frontier item cannot parent itself")
        if type(self.depth) is not int or self.depth < 0 or self.depth > 32:
            raise InvalidInputError("Invalid frontier depth")
        if (
            type(self.generation) is not int
            or self.generation < 0
            or not isinstance(self.status, SearchStatus)
        ):
            raise InvalidInputError("Invalid frontier status or generation")
        if not isinstance(self.producer, str) or not self.producer.strip() or len(self.producer) > 256:
            raise InvalidInputError("A frontier item requires a bounded producer")
        for value in (self.state_digest, self.result_digest):
            if value is not None and (
                not isinstance(value, str)
                or len(value) != 64
                or any(ch not in "0123456789abcdef" for ch in value)
            ):
                raise InvalidInputError("A frontier digest must be lowercase SHA-256")
        if self.status == SearchStatus.READY and (self.generation != 0 or self.result_digest):
            raise InvalidInputError("A ready item cannot have executed effects")
        if self.status in {SearchStatus.VERIFIED, SearchStatus.FAILED} and self.result_digest is None:
            raise InvalidInputError("Finished work requires a retained result digest")

    @property
    def identity(self) -> str:
        return content_hash({
            "node": str(self.node_id), "parent": str(self.parent_id) if self.parent_id else None,
            "depth": self.depth, "state": self.state_digest, "producer": self.producer,
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": str(self.node_id),
            "parent_id": str(self.parent_id) if self.parent_id else None,
            "depth": self.depth,
            "state_digest": self.state_digest,
            "producer": self.producer,
            "status": self.status.value,
            "generation": self.generation,
            "result_digest": self.result_digest,
        }


@dataclass(frozen=True, slots=True)
class SearchEvent:
    number: int
    kind: str
    node_id: UUID
    generation: int
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number, "kind": self.kind, "node_id": str(self.node_id),
            "generation": self.generation, "detail": self.detail,
        }


class DepthFirstSearch:
    """Pure finite search bookkeeping; external calls require a separate gateway.

    Results are accepted only for a matching claim generation. An indeterminate
    claim cannot be selected again by ``next``: operators must reconcile it using
    existing durable evidence or terminate the run. No reset-to-ready method exists.
    """

    def __init__(self, policy: SolvePolicy):
        if not isinstance(policy, SolvePolicy):
            raise InvalidInputError("Search requires a frozen SolvePolicy")
        self.policy = policy
        self.items: dict[UUID, FrontierItem] = {}
        self._ready: list[UUID] = []
        self._anchors: set[UUID] = set()
        self.events: list[SearchEvent] = []
        self.claims = 0
        self.winner: UUID | None = None

    def _event(self, kind: str, item: FrontierItem, detail: str = "") -> None:
        self.events.append(SearchEvent(len(self.events) + 1, kind, item.node_id, item.generation, detail))

    def anchor(self, item: FrontierItem) -> None:
        """Register the inert root context without charging an inference call."""
        if (not isinstance(item, FrontierItem) or item.parent_id is not None
                or item.depth != 0 or item.status != SearchStatus.READY or self.items):
            raise InvalidInputError("Root anchor must be the first unexecuted item")
        self.items[item.node_id] = item
        self._anchors.add(item.node_id)
        self._event("anchored", item)

    def offer(self, item: FrontierItem) -> None:
        if not isinstance(item, FrontierItem) or item.status != SearchStatus.READY:
            raise InvalidInputError("Only not-yet-executed frontier items may be offered")
        if item.node_id in self.items:
            raise RevisionConflictError("Duplicate node ID")
        if len(self.items) >= self.policy.max_nodes:
            raise StateMismatchError("Graph node allowance exhausted")
        if item.depth > self.policy.max_depth:
            raise StateMismatchError("Graph depth allowance exhausted")
        if item.parent_id is None:
            if item.depth != 0 or self.items:
                raise InvalidInputError("Only the first item may be a root")
        else:
            parent = self.items.get(item.parent_id)
            if parent is None or item.depth != parent.depth + 1:
                raise InvalidInputError("Missing or incorrect frontier parent")
            if parent.status in {SearchStatus.BLOCKED, SearchStatus.INDETERMINATE}:
                raise StateMismatchError("Cannot branch from blocked or uncertain work")
        self.items[item.node_id] = item
        self._ready.append(item.node_id)
        self._event("offered", item)

    def next(self) -> FrontierItem | None:
        if self.winner is not None:
            return None
        if self.claims >= self.policy.max_model_calls:
            return None
        while self._ready:
            identity = self._ready.pop()
            item = self.items[identity]
            if item.status != SearchStatus.READY:
                continue
            claimed = FrontierItem(
                item.node_id, item.parent_id, item.depth, item.state_digest,
                item.producer, SearchStatus.CLAIMED, 1,
            )
            self.items[identity] = claimed
            self.claims += 1
            self._event("claimed", claimed)
            return claimed
        return None

    def settle(self, claim: FrontierItem, outcome: SearchStatus, result_digest: str) -> FrontierItem:
        if not isinstance(claim, FrontierItem) or claim.status != SearchStatus.CLAIMED:
            raise InvalidInputError("An exact claimed snapshot is required")
        if outcome not in {SearchStatus.VERIFIED, SearchStatus.FAILED, SearchStatus.BLOCKED, SearchStatus.INDETERMINATE}:
            raise InvalidInputError("Invalid terminal observation")
        current = self.items.get(claim.node_id)
        if current != claim or self.winner is not None:
            raise RevisionConflictError("The claim is stale or this search already completed")
        updated = FrontierItem(
            current.node_id, current.parent_id, current.depth, current.state_digest,
            current.producer, outcome, current.generation, result_digest,
        )
        self.items[current.node_id] = updated
        if outcome == SearchStatus.VERIFIED:
            self.winner = updated.node_id
        self._event("settled", updated, outcome.value)
        if outcome == SearchStatus.FAILED and self._ready:
            self._event("backtracked", updated, "resume another ready branch")
        return updated

    def quarantine(self, claim: FrontierItem, receipt_digest: str) -> FrontierItem:
        """Record that an external effect may have happened, without guessing it did not."""
        return self.settle(claim, SearchStatus.INDETERMINATE, receipt_digest)

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "policy_digest": self.policy.digest,
            "winner": str(self.winner) if self.winner else None,
            "claim_count": self.claims,
            "max_model_calls": self.policy.max_model_calls,
            "queued": sum(self.items[i].status == SearchStatus.READY for i in self._ready),
            "failed": sum(i.status == SearchStatus.FAILED for i in self.items.values()),
            "uncertain": sum(i.status == SearchStatus.INDETERMINATE for i in self.items.values()),
            "verified": sum(i.status == SearchStatus.VERIFIED for i in self.items.values()),
            "events": [e.to_dict() for e in self.events],
        }

    def integrity_digest(self) -> str:
        return content_hash({
            "policy": self.policy.digest,
            "items": [self.items[k].to_dict() for k in sorted(self.items)],
            "events": [e.to_dict() for e in self.events],
            "ready_order": [str(k) for k in self._ready],
            "anchors": [str(k) for k in sorted(self._anchors)],
            "claims": self.claims,
            "winner": str(self.winner) if self.winner else None,
        })
