"""Phase 7 scoped research admission/replay against disposable real PostgreSQL."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from potbot.controller.information_needs import InformationNeedStore, NeedSpec
from potbot.controller.research_store import ResearchStore
from potbot.domain.errors import RevisionConflictError, ScopeDeniedError, StateMismatchError
from potbot.research.contracts import (
    ResearchAction,
    ResearchCategory,
    ResearchOperation,
    ResearchQuery,
    ResearchScope,
)
from potbot.research.repository import SnapshotResearchAdapter

from .test_coding_pipeline import setup

pytestmark = pytest.mark.postgres


def _setup(app_env, tmp_path):
    rig = setup(app_env, tmp_path)
    need = InformationNeedStore(app_env.db).create(
        app_env.context,
        rig.episode_id,
        NeedSpec(
            question="Which source file is relevant?",
            pending_decision="Select a bounded implementation",
            alternatives=("inspect", "stop"),
            required_evidence=("exact repository source bytes",),
            resolution_criteria=("inspected source excerpt",),
            scope="pinned repository state",
        ),
        "phase7-need",
    )
    with app_env.db.transaction(app_env.context) as conn:
        pin = conn.execute(
            "SELECT r.content_hash FROM task_semantic_pins p "
            "JOIN semantic_releases r ON r.workspace_id=p.workspace_id AND r.id=p.release_id "
            "WHERE p.workspace_id=%s AND p.episode_id=%s",
            (app_env.context.workspace_id, rig.episode_id),
        ).fetchone()
    assert pin is not None
    scope = ResearchScope(
        app_env.context.workspace_id,
        rig.episode_id,
        rig.contract_id,
        UUID(need["id"]),
        UUID(need["current_revision_id"]),
        rig.state["id"],
        pin["content_hash"],
    )
    query = ResearchQuery(
        ResearchCategory.REPOSITORY,
        ResearchOperation.READ_FILE,
        "calculator.py",
        "whole_file",
        (),
        "Read the real calculator implementation",
        "Choose an implementation",
        "Source bytes with a digest",
        "Inspected source or an explicit miss",
        "Leave need open",
    )
    return rig, scope, ResearchAction(uuid4(), scope, query)


def test_scoped_action_dedup_idempotency_and_content_integrity(app_env, tmp_path):
    _rig, scope, action = _setup(app_env, tmp_path)
    store = ResearchStore(app_env.db)
    created = store.admit(app_env.context, action, "research-A")
    assert created["action_id"] == str(action.action_id)
    assert store.admit(app_env.context, action, "research-A") == created
    assert store.admit(app_env.context, replace(action, action_id=uuid4()), "research-B")["reused"]
    assert store.inspect(app_env.context, action.action_id) == action.to_dict()
    with pytest.raises(RevisionConflictError):
        store.admit(
            app_env.context,
            replace(action, query=replace(action.query, max_results=9)),
            "research-A",
        )
    with pytest.raises(ScopeDeniedError):
        ResearchStore(app_env.other_db).inspect(app_env.other_context, action.action_id)
    with pytest.raises(RevisionConflictError):
        store.admit(
            app_env.context,
            replace(action, scope=replace(scope, need_revision_id=uuid4()), action_id=uuid4()),
            "research-C",
        )


def test_observations_bind_the_exact_repository_artifact(app_env, tmp_path):
    rig, scope, action = _setup(app_env, tmp_path)
    store = ResearchStore(app_env.db, rig.artifacts)
    store.admit(app_env.context, action, "research-A")
    with app_env.db.transaction(app_env.context) as conn:
        state = conn.execute(
            "SELECT content_digest,snapshot_artifact_id,manifest FROM repository_states "
            "WHERE workspace_id=%s AND id=%s",
            (app_env.context.workspace_id, rig.state["id"]),
        ).fetchone()
    assert state is not None
    snapshot = rig.states.snapshot(app_env.context, scope.repository_state_id)
    observed = SnapshotResearchAdapter(
        snapshot,
        repository_state_id=scope.repository_state_id,
        workspace_id=app_env.context.workspace_id,
        task_id=scope.task_id,
    ).inspect(action)[0]
    recorded = store.record_snapshot_observation(
        app_env.context,
        observed,
        state["snapshot_artifact_id"],
        "observe-A",
    )
    assert (
        store.record_snapshot_observation(
            app_env.context,
            observed,
            state["snapshot_artifact_id"],
            "observe-A",
        )
        == recorded
    )
    with pytest.raises(RevisionConflictError):
        store.record_snapshot_observation(
            app_env.context,
            replace(observed, content_digest="f" * 64),
            state["snapshot_artifact_id"],
            "observe-B",
        )
    with pytest.raises(RevisionConflictError):
        store.record_snapshot_observation(
            app_env.context,
            observed,
            uuid4(),
            "observe-C",
        )

    with pytest.raises(StateMismatchError):
        store.record_snapshot_observation(
            app_env.context,
            replace(observed, excerpt="fabricated contents"),
            state["snapshot_artifact_id"],
            "observe-D",
        )
