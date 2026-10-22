"""Exact external-source catalogue and transport preflight for Phase 7.

A catalogue is *configuration*, not network authority. A trusted controller
must separately verify reservation, disclosure and egress-policy identities
against durable records before passing a grant to this module. The returned
permit is consumed only by the existing bounded HTTP transport through a
qualified proxy/network namespace. No source code or downloaded text is run.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.domain.serialization import content_hash
from potbot.research.contracts import (
    ObservationStatus,
    ResearchAction,
    ResearchCategory,
    ResearchObservation,
    ResearchOperation,
)
from potbot.research.http_transport import FetchPermit, FetchResult, FetchStatus

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_ID = re.compile(r"[a-z][a-z0-9_-]{0,79}\Z")


def _digest(value: str, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise InvalidInputError(f"{label} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class ResearchSource:
    """One exact operator-selected source, never a model-composed URL."""

    source_id: str
    category: ResearchCategory
    exact_url: str
    declared_revision: str
    max_response_bytes: int
    egress_policy_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not _SOURCE_ID.fullmatch(self.source_id):
            raise InvalidInputError("source ID must be a bounded literal identifier")
        if not isinstance(self.category, ResearchCategory) or self.category not in {
            ResearchCategory.DOCUMENTATION,
            ResearchCategory.EXAMPLE_CODE,
            ResearchCategory.MCP,
        }:
            raise InvalidInputError("source catalogue supports external source categories only")
        if not isinstance(self.exact_url, str) or not self.exact_url or len(self.exact_url) > 1024:
            raise InvalidInputError("external source URL must be bounded")
        if (
            not isinstance(self.declared_revision, str)
            or not self.declared_revision.strip()
            or len(self.declared_revision) > 200
            or any(ord(char) < 32 for char in self.declared_revision)
        ):
            raise InvalidInputError("source revision must be explicit and bounded")
        if type(self.max_response_bytes) is not int or not 1 <= self.max_response_bytes <= 262144:
            raise InvalidInputError("source response allowance is invalid")
        _digest(self.egress_policy_digest, "egress policy")
        # Reuse the transport's authoritative HTTPS/hostname/redirect rules.
        # UUID(int=0) values are validation sentinels, never issued permits.
        FetchPermit(
            exact_url=self.exact_url,
            reservation_id=UUID(int=0),
            action_id=UUID(int=0),
            disclosure_decision_id=UUID(int=0),
            max_bytes=self.max_response_bytes,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "category": self.category.value,
            "exact_url": self.exact_url,
            "declared_revision": self.declared_revision,
            "max_response_bytes": self.max_response_bytes,
            "egress_policy_digest": self.egress_policy_digest,
        }

    @property
    def fingerprint(self) -> str:
        return content_hash({"schema_version": 1, **self.to_dict()})


@dataclass(frozen=True, slots=True)
class ResearchFetchGrant:
    """Identities from an independently authenticated controller admission.

    Constructing this value is NOT sufficient authorization; callers must read
    and verify the corresponding reservation, disclosure decision and deployed
    egress policy from the controller's own records before invoking transport.
    """

    action_id: UUID
    action_identity: str
    catalog_digest: str
    source_id: str
    reservation_id: UUID
    disclosure_decision_id: UUID
    egress_policy_digest: str

    def __post_init__(self) -> None:
        for name in ("action_id", "reservation_id", "disclosure_decision_id"):
            if not isinstance(getattr(self, name), UUID):
                raise InvalidInputError(f"{name} requires a UUID")
        for name in ("action_identity", "catalog_digest", "egress_policy_digest"):
            _digest(getattr(self, name), name)
        if not isinstance(self.source_id, str) or not _SOURCE_ID.fullmatch(self.source_id):
            raise InvalidInputError("grant source ID must be a literal source identifier")


class ResearchSourceCatalog:
    """Immutable, content-addressed set of operator-selected exact URLs."""

    def __init__(self, sources: Iterable[ResearchSource]):
        selected = tuple(sources)
        if (
            not selected
            or len(selected) > 128
            or any(not isinstance(source, ResearchSource) for source in selected)
        ):
            raise InvalidInputError("source catalogue requires 1 to 128 typed sources")
        by_id = {source.source_id: source for source in selected}
        if len(by_id) != len(selected):
            raise InvalidInputError("source catalogue contains duplicate source identities")
        self._sources = by_id
        self.fingerprint = content_hash(
            {
                "schema_version": 1,
                "sources": [by_id[name].to_dict() for name in sorted(by_id)],
            }
        )

    def preflight(self, action: ResearchAction, grant: ResearchFetchGrant) -> FetchPermit:
        """Bind source/versions/limits; never verify grants or perform egress here."""
        if not isinstance(action, ResearchAction) or not isinstance(grant, ResearchFetchGrant):
            raise InvalidInputError("external-source preflight requires typed inputs")
        if grant.action_id != action.action_id or grant.action_identity != action.identity:
            raise ScopeDeniedError("source grant does not bind the exact admitted action")
        if grant.catalog_digest != self.fingerprint:
            raise RevisionConflictError("source grant was issued for another catalogue")
        source = self._sources.get(grant.source_id)
        if source is None:
            raise ScopeDeniedError("external source is not present in the pinned catalogue")
        query = action.query
        if (
            query.category != source.category
            or query.operation != ResearchOperation.QUERY_SOURCE
            or query.locator != source.source_id
        ):
            raise ScopeDeniedError("research action cannot select another source or category")
        if query.target_version != source.declared_revision:
            raise RevisionConflictError("source revision differs from the admitted research query")
        if grant.egress_policy_digest != source.egress_policy_digest:
            raise RevisionConflictError("source grant uses another deployed egress policy")
        if query.max_source_bytes > source.max_response_bytes:
            raise InvalidInputError("research query exceeds this source's byte allowance")
        return FetchPermit(
            exact_url=source.exact_url,
            action_id=action.action_id,
            reservation_id=grant.reservation_id,
            disclosure_decision_id=grant.disclosure_decision_id,
            max_bytes=query.max_source_bytes,
        )

    def observe(
        self,
        action: ResearchAction,
        grant: ResearchFetchGrant,
        result: FetchResult,
        *,
        retrieved_at: datetime,
    ) -> ResearchObservation:
        """Project transport bytes without claiming source reliability or licence."""
        permit = self.preflight(action, grant)
        if not isinstance(result, FetchResult):
            raise InvalidInputError("observation requires a typed transport outcome")
        if result.action_id != permit.action_id or result.request_url != permit.exact_url:
            raise ScopeDeniedError("transport result does not match the pinned research action")
        source = self._sources[grant.source_id]
        digest = result.sha256
        excerpt: str | None = None
        status = ObservationStatus.UNAVAILABLE
        complete = False
        if result.status == FetchStatus.FETCHED:
            if result.data is None or len(result.data) > permit.max_bytes:
                raise InvalidInputError("transport exceeded the research source's byte allowance")
            complete = True
            try:
                decoded = result.data.decode("utf-8")
            except UnicodeDecodeError:
                status = ObservationStatus.NON_TEXT
            else:
                if not decoded:
                    status = ObservationStatus.NO_MATCH
                elif "\x00" in decoded or any(
                    ord(char) < 32 and char not in "\t\n" for char in decoded
                ):
                    status = ObservationStatus.NON_TEXT
                else:
                    status = ObservationStatus.INSPECTED
                    excerpt = decoded
        elif result.status == FetchStatus.RESPONSE_TOO_LARGE:
            status = ObservationStatus.OVER_LIMIT
        return ResearchObservation(
            action_id=action.action_id,
            status=status,
            requested_locator=source.source_id,
            resolved_locator=source.exact_url,
            source_revision=source.fingerprint,
            retrieved_at=retrieved_at,
            content_digest=digest,
            excerpt=excerpt,
            line_start=None,
            line_end=None,
            scan_complete=complete,
        )
