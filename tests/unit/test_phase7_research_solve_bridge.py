"""Pure, authority-free Phase 7 knowledge-view transformation tests."""

from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.controller.research_solve_bridge import observation_view
from potbot.domain.errors import InvalidInputError, RevisionConflictError
from potbot.domain.search_graph import KnowledgeView
from potbot.research.contracts import ResearchScope


def _values():
    ids = [uuid4() for _ in range(6)]
    scope = ResearchScope(*ids, "a" * 64)
    view = KnowledgeView(
        scope.workspace_id,
        scope.task_id,
        scope.contract_revision_id,
        scope.repository_state_id,
        scope.semantic_release_digest,
        ("b" * 64,),
        ("c" * 64,),
    )
    return scope, view


def test_observation_view_only_adds_source_evidence_and_preserves_prior_information():
    scope, parent = _values()
    transformed = observation_view(parent, scope, "d" * 64)
    assert transformed.evidence_digests == ("b" * 64, "d" * 64)
    assert transformed.source_snapshot_digests == parent.source_snapshot_digests
    assert transformed.repository_state_id == parent.repository_state_id
    assert transformed.task_id == parent.task_id
    assert parent.evidence_digests == ("b" * 64,)
    assert observation_view(transformed, scope, "d" * 64) == transformed
    assert transformed.digest != parent.digest


@pytest.mark.parametrize(
    "field",
    ("workspace_id", "task_id", "contract_revision_id", "repository_state_id"),
)
def test_rejects_cross_scope_or_different_revision(field):
    scope, parent = _values()
    changed = replace(scope, **{field: uuid4()})
    with pytest.raises(RevisionConflictError):
        observation_view(parent, changed, "d" * 64)


def test_rejects_changed_release_and_invalid_event_digest():
    scope, parent = _values()
    with pytest.raises(RevisionConflictError):
        observation_view(parent, replace(scope, semantic_release_digest="e" * 64), "d" * 64)
    for event_hash in ("d" * 63, "D" * 64, "z" * 64, ""):
        with pytest.raises(InvalidInputError):
            observation_view(parent, scope, event_hash)


def test_untrusted_raw_values_never_become_research_knowledge():
    scope, parent = _values()
    with pytest.raises(InvalidInputError):
        observation_view(parent, scope.to_dict(), "e" * 64)
    with pytest.raises(InvalidInputError):
        observation_view(parent.digest, scope, "e" * 64)
