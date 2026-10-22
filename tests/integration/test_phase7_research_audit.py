"""Real PostgreSQL read-only source-provenance audit of the Phase 7 worker."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from potbot.controller.research_worker import LocalResearchWorker
from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.persistence.artifacts import ArtifactStore

from potbot.controller.research_audit import ResearchAuditStore

from .test_phase7_research_worker import _queued

pytestmark = pytest.mark.postgres


def test_audit_exact_snapshot_output_and_receipt(app_env, tmp_path):
    rig, _scope, _action, store, queued = _queued(app_env, tmp_path)
    run_id = UUID(queued["run_id"])
    audit = ResearchAuditStore(app_env.db, rig.artifacts)
    with pytest.raises(StateMismatchError):
        audit.audit(app_env.context, run_id)
    result = LocalResearchWorker(store).run_once(app_env.context, "audit-worker")
    assert result is not None and result["state"] == "succeeded"
    qualified = audit.audit(app_env.context, run_id)
    assert qualified["qualification"] == "verified_immutable_repository_source_only"
    assert qualified["observation_count"] == 1
    assert qualified["receipt_digest"] == result["receipt_digest"]
    assert qualified["need_resolved"] is False
    assert qualified["patch_verified"] is False
    with pytest.raises(ScopeDeniedError):
        ResearchAuditStore(
            app_env.other_db, ArtifactStore(app_env.other_db, rig.artifacts.backend)
        ).audit(app_env.other_context, run_id)
    with pytest.raises(ScopeDeniedError):
        audit.audit(app_env.context, uuid4())


def test_audit_rejects_corrupted_artifact_stream(app_env, tmp_path, monkeypatch):
    rig, _scope, _action, store, queued = _queued(app_env, tmp_path)
    result = LocalResearchWorker(store).run_once(app_env.context, "audit-tamper")
    assert result is not None and result["state"] == "succeeded"
    audit = ResearchAuditStore(app_env.db, rig.artifacts)
    existing_read = rig.artifacts.read_in_transaction
    result_id = UUID(result["result_artifact_id"])

    def substituted(connection, context, artifact_id):
        raw = existing_read(connection, context, artifact_id)
        return b"{}" if artifact_id == result_id else raw

    monkeypatch.setattr(rig.artifacts, "read_in_transaction", substituted)
    with pytest.raises(StateMismatchError):
        audit.audit(app_env.context, UUID(queued["run_id"]))
