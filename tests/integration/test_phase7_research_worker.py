"""Phase 7 authentic snapshot worker against disposable real PostgreSQL."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from potbot.controller.research_store import ResearchStore
from potbot.controller.research_worker import LocalResearchWorker, ResearchRunStore
from potbot.domain.errors import (
    CapabilityUnavailableError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.persistence.artifacts import ArtifactStore
from potbot.research.contracts import (
    ResearchCategory,
    ResearchOperation,
)
from potbot.research.repository import SnapshotResearchAdapter

from .test_phase7_research_store import _setup

pytestmark = pytest.mark.postgres


def _queued(app_env, tmp_path):
    rig, scope, action = _setup(app_env, tmp_path)
    ResearchStore(app_env.db, rig.artifacts).admit(app_env.context, action, "phase7-action")
    store = ResearchRunStore(app_env.db, rig.artifacts)
    row = store.queue(app_env.context, action.action_id, "phase7-queue")
    return rig, scope, action, store, row


def test_local_worker_captures_exact_bytes_and_retains_artifact_event_receipt(app_env, tmp_path):
    rig, scope, action, store, row = _queued(app_env, tmp_path)
    assert row["reused"] is False
    assert store.queue(app_env.context, action.action_id, "queue-second-key") == {
        "run_id": row["run_id"],
        "reused": True,
    }
    completed = LocalResearchWorker(store).run_once(app_env.context, "phase7-test-worker")
    assert completed is not None and completed["state"] == "succeeded"
    assert completed["observation_count"] == 1
    assert completed["need_resolved"] is False
    assert LocalResearchWorker(store).run_once(app_env.context, "phase7-test-worker") is None
    retained = store.inspect(app_env.context, UUID(row["run_id"]))
    assert retained["receipt_digest"] == completed["receipt_digest"]
    receipt = retained["receipt"]
    assert receipt["action_identity"] == action.identity
    assert receipt["source_revision"] == rig.state["content_digest"]
    assert receipt["need_resolved"] is False
    assert receipt["execution_authorized"] is False
    data = rig.artifacts.read(app_env.context, UUID(completed["result_artifact_id"]))
    assert b'"observations"' in data
    with app_env.db.transaction(app_env.context) as conn:
        observed = conn.execute(
            "SELECT count(*) AS n FROM research_observations WHERE workspace_id=%s AND action_id=%s",
            (app_env.context.workspace_id, action.action_id),
        ).fetchone()
        events = conn.execute(
            "SELECT count(*) AS n FROM evidence_events WHERE workspace_id=%s "
            "AND episode_id=%s AND event_type='repository_research_observed'",
            (app_env.context.workspace_id, scope.task_id),
        ).fetchone()
    assert observed["n"] == 1
    assert events["n"] == 1


def test_forged_text_rejected_even_with_genuine_file_digest(app_env, tmp_path):
    rig, scope, action, store, _row = _queued(app_env, tmp_path)
    lease = store.claim(app_env.context, "attestation-worker")
    assert lease is not None
    actual = SnapshotResearchAdapter(
        rig.states.snapshot(app_env.context, scope.repository_state_id),
        repository_state_id=scope.repository_state_id,
        workspace_id=app_env.context.workspace_id,
        task_id=scope.task_id,
    ).inspect(action)
    assert actual[0].excerpt is not None
    fake = (replace(actual[0], excerpt="verified file digest, fabricated text"),)
    with pytest.raises(StateMismatchError):
        store.complete(app_env.context, lease, fake)
    assert store.inspect(app_env.context, lease.run_id)["state"] == "running"
    good = store.complete(app_env.context, lease, actual)
    assert good["state"] == "succeeded"
    with pytest.raises(RevisionConflictError):
        store.complete(app_env.context, lease, actual)


def test_stale_task_is_blocked_before_claim_and_never_auto_requeued(app_env, tmp_path):
    rig, scope, action, store, row = _queued(app_env, tmp_path)
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "UPDATE episodes SET status='paused' WHERE workspace_id=%s AND id=%s",
            (app_env.context.workspace_id, scope.task_id),
        )
    assert store.claim(app_env.context, "stale-worker") is None
    assert store.inspect(app_env.context, UUID(row["run_id"]))["state"] == "blocked"
    assert store.claim(app_env.context, "stale-worker") is None


def test_expired_lease_is_quarantined_and_cannot_publish(app_env, tmp_path):
    rig, scope, action, store, _row = _queued(app_env, tmp_path)
    lease = store.claim(app_env.context, "expiring-worker")
    assert lease is not None
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "UPDATE research_runs SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE workspace_id=%s AND id=%s",
            (app_env.context.workspace_id, lease.run_id),
        )
    assert store.quarantine_expired(app_env.context) == 1
    assert store.quarantine_expired(app_env.context) == 0
    assert store.inspect(app_env.context, lease.run_id)["state"] == "indeterminate"
    with pytest.raises(RevisionConflictError):
        store.block(app_env.context, lease, "cancelled")
    assert store.claim(app_env.context, "new-worker") is None


def test_external_action_needs_authorised_network_worker_and_scope_isolated(app_env, tmp_path):
    rig, scope, action, store, row = _queued(app_env, tmp_path)
    external = replace(
        action,
        action_id=uuid4(),
        query=replace(
            action.query,
            category=ResearchCategory.DOCUMENTATION,
            operation=ResearchOperation.QUERY_SOURCE,
            locator="docs-python",
            search_text="TaskGroup docs",
            target_version="3.13",
        ),
    )
    ResearchStore(app_env.db).admit(app_env.context, external, "external-admission")
    with pytest.raises(CapabilityUnavailableError):
        store.queue(app_env.context, external.action_id, "external-dispatch")
    with pytest.raises(ScopeDeniedError):
        ResearchRunStore(
            app_env.other_db, ArtifactStore(app_env.other_db, rig.artifacts.backend)
        ).inspect(app_env.other_context, UUID(row["run_id"]))
