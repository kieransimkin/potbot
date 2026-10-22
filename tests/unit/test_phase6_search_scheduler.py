"""Finite controller policy tests: no model/network/subprocess side effects."""
from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.controller.search_scheduler import (
    DepthFirstSearch, FrontierItem, SearchStatus,
)
from potbot.domain.errors import InvalidInputError, RevisionConflictError, StateMismatchError
from potbot.domain.search_graph import SolvePolicy

D = "a" * 64


def policy(**options):
    params = dict(workspace_id=uuid4(), task_id=uuid4(), contract_revision_id=uuid4(),
                  base_state_id=uuid4(), semantic_release_digest=D,
                  reasoning_models=("reasoner",), coding_models=("coder",))
    params.update(options)
    return SolvePolicy(**params)


def roots(p):
    engine = DepthFirstSearch(p)
    root = FrontierItem(uuid4(), None, 0, D, "controller")
    engine.offer(root)
    return engine, root


def test_depth_first_and_verified_stop():
    engine, root = roots(policy())
    a = FrontierItem(uuid4(), root.node_id, 1, "b" * 64, "a")
    b = FrontierItem(uuid4(), root.node_id, 1, "c" * 64, "b")
    engine.offer(a)
    engine.offer(b)
    claim = engine.next()
    assert claim.node_id == b.node_id
    engine.settle(claim, SearchStatus.FAILED, D)
    claim2 = engine.next()
    assert claim2.node_id == a.node_id
    engine.settle(claim2, SearchStatus.VERIFIED, D)
    assert engine.next() is None
    assert engine.summary()["verified"] == 1
    assert any(e.kind == "backtracked" for e in engine.events)


def test_wrong_generation_or_stale_claim_cannot_publish():
    engine, _ = roots(policy())
    claim = engine.next()
    with pytest.raises(RevisionConflictError):
        engine.settle(replace(claim, generation=2), SearchStatus.VERIFIED, D)
    engine.settle(claim, SearchStatus.FAILED, D)
    with pytest.raises(RevisionConflictError):
        engine.settle(claim, SearchStatus.VERIFIED, D)


def test_indeterminate_is_never_auto_replayed():
    engine, root = roots(policy())
    claim = engine.next()
    engine.quarantine(claim, "b" * 64)
    assert engine.next() is None
    assert engine.summary()["uncertain"] == 1
    with pytest.raises(StateMismatchError):
        engine.offer(FrontierItem(uuid4(), root.node_id, 1, D, "coder"))


def test_bound_model_calls_and_nodes():
    engine, root = roots(policy(max_model_calls=1, max_depth=1, max_nodes=2))
    child = FrontierItem(uuid4(), root.node_id, 1, "b" * 64, "coder")
    engine.offer(child)
    with pytest.raises(StateMismatchError):
        engine.offer(FrontierItem(uuid4(), root.node_id, 1, "c" * 64, "coder"))
    engine.next()
    assert engine.next() is None


def test_invalid_parent_and_payload_rejected():
    engine, root = roots(policy())
    with pytest.raises(InvalidInputError):
        engine.offer(FrontierItem(uuid4(), uuid4(), 1, D, "coder"))
    with pytest.raises(InvalidInputError):
        engine.offer(FrontierItem(uuid4(), root.node_id, 2, D, "coder"))
    with pytest.raises(InvalidInputError):
        FrontierItem(uuid4(), None, True, D, "coder")
    with pytest.raises(InvalidInputError):
        FrontierItem(uuid4(), None, 0, "foo", "coder")


def test_event_digest_and_duplicate_node():
    engine, root = roots(policy())
    assert engine.integrity_digest() == engine.integrity_digest()
    with pytest.raises(RevisionConflictError):
        engine.offer(root)
    claim = engine.next()
    assert engine.integrity_digest() != claim.identity
    with pytest.raises(InvalidInputError):
        engine.settle(claim, SearchStatus.READY, D)
