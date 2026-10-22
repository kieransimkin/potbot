"""Phase 6 durable search / fencing acceptance with real PostgreSQL.

Not part of the offline-only test gate; requires POTBOT_TEST_DSN and a
PostgreSQL server with pgvector, using the disposable app_env fixture.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from potbot.controller.solve_store import SolveStore
from potbot.domain.errors import RevisionConflictError, ScopeDeniedError, StateMismatchError
from potbot.domain.search_graph import EdgeKind, KnowledgeView, NodeKind, SolvePolicy

from .test_coding_pipeline import setup

pytestmark = pytest.mark.postgres


def _run(app_env, tmp_path: Path):
    rig = setup(app_env, tmp_path)
    with app_env.db.transaction(app_env.context) as conn:
        release = conn.execute(
            "SELECT r.content_hash FROM task_semantic_pins p JOIN semantic_releases r "
            "ON r.workspace_id=p.workspace_id AND r.id=p.release_id "
            "WHERE p.workspace_id=%s AND p.episode_id=%s",
            (app_env.context.workspace_id, rig.episode_id),
        ).fetchone()
    assert release is not None
    policy = SolvePolicy(
        app_env.context.workspace_id, rig.episode_id, rig.contract_id, rig.state["id"],
        release["content_hash"], ("reasoner-fixture",), ("coder-fixture-a", "coder-fixture-b"),
    )
    root = KnowledgeView(
        policy.workspace_id, policy.task_id, policy.contract_revision_id,
        policy.base_state_id, policy.semantic_release_digest, (),
    )
    store = SolveStore(app_env.db)
    run = store.create(app_env.context, policy, root, "start-solve")
    return rig, store, policy, root, run


def test_durable_policy_replay_and_workspace_scope(app_env, tmp_path: Path):
    _rig, store, policy, root, run = _run(app_env, tmp_path)
    assert store.create(app_env.context, policy, root, "start-solve") == run
    with pytest.raises(RevisionConflictError):
        store.create(app_env.context, replace(policy, max_nodes=128), root, "start-solve")
    again = SolveStore(app_env.db).inspect(app_env.context, UUID(run["run_id"]))
    assert again["run"]["policy_digest"] == policy.digest
    assert again["node_count"] == 1
    assert again["nodes"][0]["kind"] == "root"
    with pytest.raises(ScopeDeniedError):
        SolveStore(app_env.other_db).inspect(app_env.other_context, UUID(run["run_id"]))


def test_graph_claim_fencing_pause_and_expiry(app_env, tmp_path: Path):
    rig, store, _policy, root, run = _run(app_env, tmp_path)
    run_id, root_id, view_id = (
        UUID(run["run_id"]), UUID(run["root_node_id"]), UUID(run["root_view_id"]),
    )
    candidate = store.add_node(
        app_env.context, run_id, root_id, view_id, NodeKind.CANDIDATE,
        "coder-fixture-a", "b" * 64, "candidate-one",
    )
    assert store.add_node(
        app_env.context, run_id, root_id, view_id, NodeKind.CANDIDATE,
        "coder-fixture-a", "b" * 64, "candidate-one",
    ) == candidate
    store.add_edge(
        app_env.context, run_id, root_id, UUID(candidate["id"]),
        EdgeKind.CONTINUES, "edge-one",
    )
    lease = store.claim(app_env.context, run_id, uuid4(), lease_seconds=45)
    assert lease is not None
    assert store.claim(app_env.context, run_id, uuid4(), lease_seconds=45) is not None
    # The same node cannot be claimed by another worker while its lease is live.
    with app_env.db.transaction(app_env.context) as conn:
        status = conn.execute(
            "SELECT status FROM solve_frontier WHERE workspace_id=%s AND run_id=%s AND node_id=%s",
            (app_env.context.workspace_id, run_id, lease.node_id),
        ).fetchone()
        assert status["status"] == "claimed"
        conn.execute(
            "UPDATE solve_frontier SET lease_until=clock_timestamp()-interval '2 seconds' "
            "WHERE workspace_id=%s AND run_id=%s AND node_id=%s",
            (app_env.context.workspace_id, run_id, lease.node_id),
        )
    quarantined = store.quarantine_expired(app_env.context, run_id)
    assert quarantined == [str(lease.node_id)]
    with pytest.raises(StateMismatchError):
        # The run has become indeterminate rather than replaying external work.
        store.finish(app_env.context, lease, "c" * 64, "late-finish")
    assert store.inspect(app_env.context, run_id)["run"]["status"] == "indeterminate"


def test_changed_task_revision_prevents_new_graph_mutations(app_env, tmp_path: Path):
    rig, store, policy, _root, run = _run(app_env, tmp_path)
    paused = rig.tasks.control(
        app_env.context, rig.episode_id, 2, "pause", "review",
        idempotency_key="pause-phase6",
    )
    assert paused["status"] == "paused"
    with pytest.raises(RevisionConflictError):
        store.add_node(
            app_env.context, UUID(run["run_id"]), UUID(run["root_node_id"]),
            UUID(run["root_view_id"]), NodeKind.PROPOSAL,
            "reasoner-fixture", "f" * 64, "after-pause",
        )
    with pytest.raises(RevisionConflictError):
        store.claim(app_env.context, UUID(run["run_id"]), uuid4())
