"""Durable payload/event/outbox tests against real restricted PostgreSQL logins."""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.persistence.database import Database, admin_connection
from potbot.persistence.evidence import EvidenceStore, append_event
from potbot.persistence.outbox import FixtureWorker, Outbox, enqueue_job

pytestmark = pytest.mark.postgres


def artifacts(env):
    return ArtifactStore(env.db, LocalArtifactBackend(env.artifact_root))


def test_artifact_identity_scope_and_immutable_metadata(app_env) -> None:
    store = artifacts(app_env)
    first = store.put(app_env.context, b"evidence", "text/plain")
    second = store.put(app_env.context, b"evidence", "text/plain")
    assert first == second
    assert store.read(app_env.context, first["id"]) == b"evidence"
    other = ArtifactStore(app_env.other_db, store.backend)
    for identity in (first["id"], uuid4()):
        with pytest.raises(ScopeDeniedError, match="unavailable"):
            other.read(app_env.other_context, identity)
    with pytest.raises((ScopeDeniedError, RevisionConflictError)):
        with app_env.db.transaction(app_env.context) as connection:
            connection.execute("UPDATE artifacts SET byte_length=0 WHERE id=%s", (first["id"],))
    assert store.read(app_env.context, first["id"]) == b"evidence"


def test_event_provenance_causality_and_digest_survive_reconnection(app_env) -> None:
    payload = artifacts(app_env).put(app_env.context, b"request")
    first = EvidenceStore(app_env.db).user_input(app_env.context, payload["id"])
    occurrence = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    followup = EvidenceStore(app_env.db).append(
        app_env.context,
        "controller_decision",
        occurred_at=occurrence,
        causation_id=first["id"],
        correlation_id=first["correlation_id"],
        details={"next_action": "search_existing_tools"},
    )
    reconnected = EvidenceStore(Database(app_env.db.dsn, app_env.schema))
    assert reconnected.read(app_env.context, followup["id"]) == followup
    assert followup["occurred_at"] == occurrence
    assert first["producer_id"] == str(app_env.context.principal_id)
    assert followup["causation_id"] == first["id"]
    with pytest.raises((ScopeDeniedError, RevisionConflictError)):
        with app_env.db.transaction(app_env.context) as connection:
            connection.execute("DELETE FROM evidence_events WHERE id=%s", (first["id"],))
    assert reconnected.read(app_env.context, first["id"]) == first


def test_model_claim_cannot_become_tool_evidence_and_foreign_payload_rejected(app_env) -> None:
    with pytest.raises(InvalidInputError):
        EvidenceStore(app_env.db).append(
            app_env.context,
            "tool_observation",
            producer_kind="model",
            producer_id="model",
            deployment_id=uuid4(),
            details={"verified": True},
        )
    payload = ArtifactStore(app_env.other_db, LocalArtifactBackend(app_env.artifact_root)).put(
        app_env.other_context, b"private"
    )
    for identity in (payload["id"], uuid4()):
        with pytest.raises(ScopeDeniedError):
            EvidenceStore(app_env.db).user_input(app_env.context, identity)
    with app_env.db.transaction(app_env.context) as connection:
        assert connection.execute("SELECT count(*) AS n FROM evidence_events").fetchone()["n"] == 0


def test_outbox_rollback_and_replay_keep_one_event_and_job(app_env) -> None:
    with pytest.raises(RuntimeError):
        with app_env.db.transaction(app_env.context) as connection:
            event = append_event(connection, app_env.context, "work_requested")
            enqueue_job(
                connection,
                app_env.context,
                job_key="rollback",
                job_type="fixture",
                event_id=event["id"],
            )
            raise RuntimeError("fixture failure before commit")
    outbox = Outbox(app_env.db)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = [
            executor.submit(
                outbox.publish,
                app_env.context,
                key="stable",
                event_type="work_requested",
                details={},
            )
            for _ in range(2)
        ]
        result = [future.result() for future in pending]
    assert result[0] == result[1]
    with app_env.db.transaction(app_env.context) as connection:
        assert connection.execute("SELECT count(*) AS n FROM evidence_events").fetchone()["n"] == 1
        assert connection.execute("SELECT count(*) AS n FROM outbox_jobs").fetchone()["n"] == 1
    with pytest.raises(RevisionConflictError):
        outbox.publish(app_env.context, key="stable", event_type="work_requested", details={"x": 1})


def test_killed_publisher_leaves_durable_job_for_restarted_worker(app_env) -> None:
    program = """
import json, os, sys
from uuid import UUID
from potbot.domain.auth import WorkspaceContext
from potbot.persistence.database import Database
from potbot.persistence.outbox import Outbox
ctx=WorkspaceContext(UUID(os.environ['FIXTURE_WORKSPACE']),UUID(os.environ['FIXTURE_PRINCIPAL']))
result=Outbox(Database(os.environ['FIXTURE_DSN'])).publish(ctx,key='crash',event_type='work_requested',details={'fixture':True})
print(json.dumps(result),flush=True)
sys.stdin.read()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", program],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "FIXTURE_DSN": app_env.db.dsn,
            "FIXTURE_WORKSPACE": str(app_env.context.workspace_id),
            "FIXTURE_PRINCIPAL": str(app_env.context.principal_id),
        },
    )
    try:
        assert child.stdout is not None
        line = child.stdout.readline()
        assert line, child.stderr.read() if child.stderr else "Child did not commit"
        result = json.loads(line)
        child.kill()
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream:
                stream.close()
    outbox = Outbox(Database(app_env.db.dsn))
    worker = FixtureWorker(outbox, artifacts(app_env), "restarted-worker")
    completed = worker.run_once(app_env.context)
    assert completed["id"] == UUID(result["job_id"])
    assert completed["status"] == "completed"
    completion = json.loads(
        artifacts(app_env).read(app_env.context, completed["completion_artifact_id"])
    )
    assert completion["event_id"] == result["event_id"]
    assert worker.run_once(app_env.context) is None
    with app_env.db.transaction(app_env.context) as connection:
        assert connection.execute("SELECT count(*) AS n FROM evidence_events").fetchone()["n"] == 1


def expire(env, job_id):
    with admin_connection(env.admin_dsn, env.schema) as connection:
        connection.execute(
            "UPDATE outbox_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (job_id,),
        )


def test_expired_worker_cannot_complete_or_renew_reclaimed_job(app_env) -> None:
    outbox = Outbox(app_env.db)
    result = outbox.publish(app_env.context, key="lease", event_type="work_requested", details={})
    old = outbox.claim(app_env.context, "worker-a")
    assert old is not None
    expire(app_env, old.job_id)
    new = outbox.claim(app_env.context, "worker-b")
    assert new is not None and new.generation == old.generation + 1
    artifact = artifacts(app_env).put(app_env.context, b"done")
    for operation in (
        lambda: outbox.complete(app_env.context, old, artifact["id"]),
        lambda: outbox.renew(app_env.context, old),
        lambda: outbox.fail(app_env.context, old, error_code="timeout", retryable=True),
    ):
        with pytest.raises(RevisionConflictError):
            operation()
    assert outbox.renew(app_env.context, new) >= new.lease_until
    assert outbox.complete(app_env.context, new, artifact["id"])["status"] == "completed"
    assert outbox.inspect(app_env.context, UUID(result["job_id"]))["generation"] == new.generation


def test_attempt_limits_terminal_errors_and_foreign_completion(app_env) -> None:
    outbox = Outbox(app_env.db)
    result = outbox.publish(
        app_env.context, key="bounded", event_type="work_requested", details={}, max_attempts=2
    )
    first = outbox.claim(app_env.context, "worker-a")
    expire(app_env, first.job_id)
    second = outbox.claim(app_env.context, "worker-b")
    assert second.attempt == 2
    expire(app_env, second.job_id)
    assert outbox.claim(app_env.context, "worker-c") is None
    assert outbox.inspect(app_env.context, UUID(result["job_id"]))["status"] == "failed"
    outbox.publish(app_env.context, key="terminal", event_type="work_requested", details={})
    lease = outbox.claim(app_env.context, "worker-d")
    foreign = ArtifactStore(app_env.other_db, LocalArtifactBackend(app_env.artifact_root)).put(
        app_env.other_context, b"foreign"
    )
    with pytest.raises(ScopeDeniedError):
        outbox.complete(app_env.context, lease, foreign["id"])
    failed = outbox.fail(app_env.context, lease, error_code="schema_violation", retryable=False)
    assert failed["status"] == "failed"
    assert outbox.claim(app_env.context, "worker-e") is None


def test_lease_expiring_during_publication_cannot_commit(app_env, monkeypatch) -> None:
    outbox = Outbox(app_env.db)
    outbox.publish(app_env.context, key="expiry", event_type="work_requested", details={})
    lease = outbox.claim(app_env.context, "slow-worker", lease_seconds=1)
    store = artifacts(app_env)
    publish = store.backend.publish

    def slow_publish(workspace_id, data):
        result = publish(workspace_id, data)
        time.sleep(1.1)
        return result

    monkeypatch.setattr(store.backend, "publish", slow_publish)
    with pytest.raises(RevisionConflictError):
        outbox.complete_payload(app_env.context, lease, store, b"slow result")
    with app_env.db.transaction(app_env.context) as connection:
        assert connection.execute("SELECT count(*) AS n FROM artifacts").fetchone()["n"] == 0
    assert outbox.inspect(app_env.context, lease.job_id)["status"] == "running"
    replacement = outbox.claim(app_env.context, "replacement-worker")
    assert replacement.generation == lease.generation + 1
