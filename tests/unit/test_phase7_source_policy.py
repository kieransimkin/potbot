"""Phase 7 external source pinning: no implicit egress or evidence promotion."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.research.contracts import (
    ObservationStatus,
    ResearchAction,
    ResearchCategory,
    ResearchOperation,
    ResearchQuery,
    ResearchScope,
)
from potbot.research.http_transport import FetchResult, FetchStatus
from potbot.research.source_policy import (
    ResearchFetchGrant,
    ResearchSource,
    ResearchSourceCatalog,
)

HASH = "a" * 64
URL = "https://docs.python.org/3.13/library/asyncio-task.html"


def source(**changes: object) -> ResearchSource:
    base: dict[str, object] = {
        "source_id": "python-asyncio",
        "category": ResearchCategory.DOCUMENTATION,
        "exact_url": URL,
        "declared_revision": "3.13",
        "max_response_bytes": 4096,
        "egress_policy_digest": HASH,
    }
    return ResearchSource(**{**base, **changes})  # type: ignore[arg-type]


def action(**changes: object) -> ResearchAction:
    scope = ResearchScope(uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), HASH)
    query = ResearchQuery(
        ResearchCategory.DOCUMENTATION,
        ResearchOperation.QUERY_SOURCE,
        "python-asyncio",
        "Read about TaskGroup cancellation in Python 3.13",
        (),
        "Observed documentation with exact revision",
        "Choose safe cancellation handling",
        "Retrieved source content",
        "Content inspected or classified unavailable",
        "Keep the need open",
        target_version="3.13",
        max_source_bytes=1024,
    )
    return ResearchAction(uuid4(), scope, replace(query, **changes))


def fixture():
    item = action()
    registry = ResearchSourceCatalog([source()])
    grant = ResearchFetchGrant(
        item.action_id,
        item.identity,
        registry.fingerprint,
        "python-asyncio",
        uuid4(),
        uuid4(),
        HASH,
    )
    return item, registry, grant


def result(item: ResearchAction, data: bytes, status: FetchStatus = FetchStatus.FETCHED):
    return FetchResult(
        status,
        item.action_id,
        URL,
        200 if status == FetchStatus.FETCHED else None,
        data if status == FetchStatus.FETCHED else None,
        hashlib.sha256(data).hexdigest() if status == FetchStatus.FETCHED else None,
        len(data) if status == FetchStatus.FETCHED else 0,
    )


def test_preflight_is_exactly_bound_and_cannot_perform_network():
    item, registry, grant = fixture()
    permit = registry.preflight(item, grant)
    assert permit.exact_url == URL
    assert permit.action_id == item.action_id
    assert permit.reservation_id == grant.reservation_id
    assert permit.disclosure_decision_id == grant.disclosure_decision_id
    assert permit.max_bytes == 1024


def test_catalogue_identity_is_order_independent_and_duplicate_ids_rejected():
    a = source()
    b = source(
        source_id="python-errors", exact_url="https://docs.python.org/3.13/library/exceptions.html"
    )
    assert ResearchSourceCatalog([a, b]).fingerprint == ResearchSourceCatalog([b, a]).fingerprint
    with pytest.raises(InvalidInputError):
        ResearchSourceCatalog([a, a])
    with pytest.raises(InvalidInputError):
        ResearchSourceCatalog([])


@pytest.mark.parametrize(
    "url",
    [
        "http://docs.python.org/3.13/library/asyncio.html",
        "https://127.0.0.1/private",
        "https://localhost/private",
        "https://user:pass@docs.python.org/private",
        "https://docs.python.org:8080/private",
        "https://docs.python.org/private?secret=1",
        "https://docs.python.org/private#fragment",
        "https://docs.python.org.evil.invalid/path/../private?x=1",
    ],
)
def test_source_rejects_noncompliant_transport_destinations(url):
    with pytest.raises(InvalidInputError):
        source(exact_url=url)


def test_source_requires_exact_categories_revisions_and_digest():
    with pytest.raises(InvalidInputError):
        source(category=ResearchCategory.REPOSITORY)
    with pytest.raises(InvalidInputError):
        source(category="documentation")
    with pytest.raises(InvalidInputError):
        source(declared_revision=" ")
    with pytest.raises(InvalidInputError):
        source(egress_policy_digest="junk")
    with pytest.raises(InvalidInputError):
        source(max_response_bytes=262145)


def test_source_cannot_be_selected_by_arbitrary_locator_or_cross_category():
    item, registry, grant = fixture()
    with pytest.raises(ScopeDeniedError):
        registry.preflight(replace(item, query=replace(item.query, locator="wrong")), grant)
    with pytest.raises(ScopeDeniedError):
        registry.preflight(
            replace(item, query=replace(item.query, category=ResearchCategory.MCP)), grant
        )
    with pytest.raises(ScopeDeniedError):
        registry.preflight(item, replace(grant, source_id="unregistered"))


def test_grants_reject_reformulated_queries_or_stale_configuration():
    item, registry, grant = fixture()
    with pytest.raises(ScopeDeniedError):
        registry.preflight(replace(item, action_id=uuid4()), grant)
    with pytest.raises(ScopeDeniedError):
        registry.preflight(replace(item, query=replace(item.query, search_text="changed")), grant)
    with pytest.raises(RevisionConflictError):
        registry.preflight(item, replace(grant, catalog_digest="f" * 64))
    with pytest.raises(RevisionConflictError):
        registry.preflight(item, replace(grant, egress_policy_digest="f" * 64))


def test_query_cannot_silently_upgrade_dependency_or_expand_byte_budget():
    item, registry, grant = fixture()
    changed = replace(item, query=replace(item.query, target_version="3.14"))
    changed_grant = replace(grant, action_identity=changed.identity)
    with pytest.raises(RevisionConflictError):
        registry.preflight(changed, changed_grant)
    over = replace(item, query=replace(item.query, max_source_bytes=8192))
    with pytest.raises(InvalidInputError):
        registry.preflight(over, replace(grant, action_identity=over.identity))


def test_successful_transport_observation_retains_source_fingerprint_and_unknown_license():
    item, registry, grant = fixture()
    raw = b"TaskGroup cancels sibling tasks when a task fails."
    observed = registry.observe(
        item, grant, result(item, raw), retrieved_at=datetime(2026, 10, 22, tzinfo=UTC)
    )
    assert observed.status == ObservationStatus.INSPECTED
    assert observed.excerpt == raw.decode("utf-8")
    assert observed.content_digest == hashlib.sha256(raw).hexdigest()
    assert observed.source_revision == source().fingerprint
    assert observed.requested_locator == "python-asyncio"
    assert observed.resolved_locator == URL
    assert observed.scan_complete
    assert observed.license_status == "unknown"


@pytest.mark.parametrize(
    "raw,expected",
    [
        (b"", ObservationStatus.NO_MATCH),
        (b"\xff\xfe", ObservationStatus.NON_TEXT),
        (b"binary\x00content", ObservationStatus.NON_TEXT),
    ],
)
def test_empty_and_nontext_source_results_are_not_claimed_as_evidence(raw, expected):
    item, registry, grant = fixture()
    observed = registry.observe(item, grant, result(item, raw), retrieved_at=datetime.now(UTC))
    assert observed.status == expected
    assert observed.excerpt is None


@pytest.mark.parametrize(
    "status,expected",
    [
        (FetchStatus.REDIRECT_DENIED, ObservationStatus.UNAVAILABLE),
        (FetchStatus.UNAVAILABLE, ObservationStatus.UNAVAILABLE),
        (FetchStatus.TIMED_OUT, ObservationStatus.UNAVAILABLE),
        (FetchStatus.RATE_LIMITED, ObservationStatus.UNAVAILABLE),
        (FetchStatus.RESPONSE_TOO_LARGE, ObservationStatus.OVER_LIMIT),
    ],
)
def test_transport_failures_do_not_turn_into_inspected_source(status, expected):
    item, registry, grant = fixture()
    observed = registry.observe(
        item, grant, result(item, b"", status), retrieved_at=datetime.now(UTC)
    )
    assert observed.status == expected
    assert not observed.scan_complete
    assert observed.excerpt is None


def test_forged_transport_locator_or_unqualified_bytes_are_rejected():
    item, registry, grant = fixture()
    fake = FetchResult(
        FetchStatus.FETCHED,
        item.action_id,
        "https://example.com/foreign",
        200,
        b"content",
        hashlib.sha256(b"content").hexdigest(),
        7,
    )
    with pytest.raises(ScopeDeniedError):
        registry.observe(item, grant, fake, retrieved_at=datetime.now(UTC))
    huge = b"x" * 1025
    with pytest.raises(InvalidInputError):
        registry.observe(item, grant, result(item, huge), retrieved_at=datetime.now(UTC))
