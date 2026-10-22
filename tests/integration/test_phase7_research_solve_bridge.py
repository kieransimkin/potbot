"""Source-byte-audited research becomes scoped *observation* graph knowledge only."""

from uuid import UUID, uuid4

import pytest
from potbot.controller.research_worker import LocalResearchWorker
from potbot.controller.solve_store import SolveStore
from potbot.domain.errors import RevisionConflictError, ScopeDeniedError, StateMismatchError
from potbot.domain.search_graph import KnowledgeView, SolvePolicy
from potbot.persistence.artifacts import ArtifactStore

from potbot.controller.research_solve_bridge import ResearchSolveBridge

from .test_phase7_research_worker import _queued

pytestmark = pytest.mark.postgres


def _fixture(app_env, tmp_path):
    rig, scope, action, research, queued = _queued(app_env, tmp_path)
    policy = SolvePolicy(
        scope.workspace_id,
        scope.task_id,
        scope.contract_revision_id,
        scope.repository_state_id,
        scope.semantic_release_digest,
        ("reasoner-test",),
        ("coder-test",),
    )
    root = KnowledgeView(
        policy.workspace_id,
        policy.task_id,
        policy.contract_revision_id,
        policy.base_state_id,
        policy.semantic_release_digest,
        (),
    )
    solve = SolveStore(app_env.db).create(app_env.context, policy, root, "research-link-solve")
    return rig, scope, action, research, UUID(queued["run_id"]), solve


def test_source_audit_becomes_finished_observation_without_code_execution(app_env, tmp_path):
    rig, scope, _action, research, research_id, solve = _fixture(app_env, tmp_path)
    solve_id, parent_id = UUID(solve["run_id"]), UUID(solve["root_node_id"])
    bridge = ResearchSolveBridge(app_env.db, rig.artifacts)
    with pytest.raises(StateMismatchError):
        bridge.link(app_env.context, research_id, solve_id, parent_id, "link-before-source")
    result = LocalResearchWorker(research).run_once(app_env.context, "link-worker")
    assert result is not None and result["state"] == "succeeded"
    link = bridge.link(app_env.context, research_id, solve_id, parent_id, "link-source")
    assert link["reused"] is False
    assert link["need_resolved"] is False and link["patch_verified"] is False
    assert bridge.link(app_env.context, research_id, solve_id, parent_id, "link-source") == link
    assert bridge.link(app_env.context, research_id, solve_id, parent_id, "link-again")["reused"]
    inspected = bridge.inspect(app_env.context, solve_id, research_id)
    assert inspected["link_digest"] == link["link_digest"]
    assert inspected["content"]["source_receipt_digest"] == result["receipt_digest"]
    observed = SolveStore(app_env.db).inspect(app_env.context, solve_id)
    node = next(node for node in observed["nodes"] if node["id"] == str(link["node_id"]))
    assert node["kind"] == "observation"
    assert node["frontier_state"] == "finished"
    with app_env.db.transaction(app_env.context) as conn:
        stored = conn.execute(
            "SELECT v.content,l.content AS link_body,e.content_digest AS event_digest "
            "FROM research_solve_links l JOIN solve_views v "
            "ON v.workspace_id=l.workspace_id AND v.id=l.view_id "
            "JOIN evidence_events e ON e.workspace_id=l.workspace_id AND e.id=l.evidence_event_id "
            "WHERE l.workspace_id=%s AND l.solve_run_id=%s",
            (app_env.context.workspace_id, solve_id),
        ).fetchone()
        assert stored is not None
        assert stored["event_digest"] in stored["content"]["evidence_digests"]
        assert stored["link_body"]["source_receipt_digest"] == result["receipt_digest"]
        assert stored["link_body"]["patch_verified"] is False
        assert stored["link_body"]["model_dispatched"] is False
        needs = conn.execute(
            "SELECT status FROM need_status WHERE workspace_id=%s AND need_id=%s",
            (app_env.context.workspace_id, scope.need_id),
        ).fetchone()
        assert needs["status"] == "open"


def test_cross_workspace_and_ambiguous_parent_are_denied(app_env, tmp_path):
    rig, _scope, _action, research, research_id, solve = _fixture(app_env, tmp_path)
    LocalResearchWorker(research).run_once(app_env.context, "link-worker-b")
    bridge = ResearchSolveBridge(app_env.db, rig.artifacts)
    with pytest.raises(ScopeDeniedError):
        ResearchSolveBridge(
            app_env.other_db, ArtifactStore(app_env.other_db, rig.artifacts.backend)
        ).link(
            app_env.other_context,
            research_id,
            UUID(solve["run_id"]),
            UUID(solve["root_node_id"]),
            "cross-workspace",
        )
    with pytest.raises(ScopeDeniedError):
        bridge.link(app_env.context, research_id, UUID(solve["run_id"]), uuid4(), "unknown-parent")


def test_paused_task_cannot_promote_source_even_when_audit_passes(app_env, tmp_path):
    rig, scope, _action, research, research_id, solve = _fixture(app_env, tmp_path)
    LocalResearchWorker(research).run_once(app_env.context, "link-worker-c")
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "UPDATE episodes SET status='paused' WHERE workspace_id=%s AND id=%s",
            (app_env.context.workspace_id, scope.task_id),
        )
    with pytest.raises(RevisionConflictError):
        ResearchSolveBridge(app_env.db, rig.artifacts).link(
            app_env.context,
            research_id,
            UUID(solve["run_id"]),
            UUID(solve["root_node_id"]),
            "paused-link",
        )
