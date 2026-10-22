"""Pure Phase 6 run policy, scoped evidence and graph admission checks."""

from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.domain.errors import InvalidInputError, RevisionConflictError
from potbot.domain.search_graph import (
    EdgeKind,
    GraphEdge,
    GraphNode,
    GraphProjection,
    KnowledgeView,
    NodeKind,
    SolvePolicy,
)

D = "a" * 64


def policy():
    return SolvePolicy(
        uuid4(), uuid4(), uuid4(), uuid4(), D, ("provider/reasoner",), ("provider/coder",)
    )


def view(p):
    return KnowledgeView(
        p.workspace_id,
        p.task_id,
        p.contract_revision_id,
        p.base_state_id,
        p.semantic_release_digest,
        (D,),
    )


def node(p, kind=NodeKind.ROOT, *, depth=0, v=None, payload=D):
    return GraphNode(uuid4(), kind, v or view(p), "fixture-producer", payload, depth)


def test_policy_is_immutable_and_budgeted():
    p = policy()
    assert p.digest == p.digest and p.to_dict()["reasoning_models"] == ["provider/reasoner"]
    with pytest.raises(InvalidInputError):
        replace(p, max_depth=0)
    with pytest.raises(InvalidInputError):
        replace(p, max_nodes=2)
    with pytest.raises(InvalidInputError):
        replace(p, max_money_microunits=-1)
    with pytest.raises(InvalidInputError):
        replace(p, reasoning_models=("duplicate", "duplicate"))
    with pytest.raises(InvalidInputError):
        replace(p, max_model_calls=True)


def test_knowledge_views_pin_scope_and_are_order_independent():
    p = policy()
    a = KnowledgeView(
        p.workspace_id,
        p.task_id,
        p.contract_revision_id,
        p.base_state_id,
        D,
        ("b" * 64, "c" * 64),
    )
    b = replace(a, evidence_digests=tuple(reversed(a.evidence_digests)))
    assert a.digest == b.digest
    assert replace(a, repository_state_id=uuid4()).digest != a.digest
    with pytest.raises(InvalidInputError):
        replace(a, evidence_digests=(D, D))


def test_graph_dedup_and_cycles_are_rejected():
    p = policy()
    g = GraphProjection(p)
    root = node(p)
    child = node(p, NodeKind.PROPOSAL, depth=1, payload="b" * 64)
    assert g.add_node(root) == root.id
    assert g.add_node(replace(root, id=uuid4(), producer="other-model")) == root.id
    assert g.add_node(child) == child.id
    g.add_edge(GraphEdge(root.id, child.id, EdgeKind.DERIVED_FROM))
    g.add_edge(GraphEdge(root.id, child.id, EdgeKind.DERIVED_FROM))
    assert len(g.edges) == 1
    with pytest.raises(InvalidInputError):
        g.add_edge(GraphEdge(child.id, root.id, EdgeKind.DERIVED_FROM))


def test_stale_revision_and_cross_state_import_are_rejected():
    p = policy()
    g = GraphProjection(p)
    root = node(p)
    g.add_node(root)
    with pytest.raises(RevisionConflictError):
        g.add_node(node(p, v=replace(view(p), contract_revision_id=uuid4())))
    different = node(p, NodeKind.OBSERVATION, v=replace(view(p), repository_state_id=uuid4()))
    g.add_node(different)
    with pytest.raises(InvalidInputError):
        g.add_edge(GraphEdge(root.id, different.id, EdgeKind.IMPORTS))
    g.add_edge(GraphEdge(root.id, different.id, EdgeKind.CONTINUES))


def test_no_type_confusion_in_identity():
    p = policy()
    with pytest.raises(InvalidInputError):
        node(p, depth=True)
    with pytest.raises(InvalidInputError):
        GraphEdge(uuid4(), uuid4(), "not-an-edge")
