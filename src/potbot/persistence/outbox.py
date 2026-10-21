"""Transactional outbox and at-least-once workers with generation fencing."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.domain.serialization import canonical_json
from potbot.persistence.evidence import _scoped_reference, append_event

if TYPE_CHECKING:
    from potbot.persistence.artifacts import ArtifactStore
    from potbot.persistence.database import Database


def enqueue_job(
    connection: Any,
    context: WorkspaceContext,
    *,
    job_key: str,
    job_type: str,
    event_id: UUID,
    payload_id: UUID | None = None,
    policy_version: str = "fixture-v1",
    schema_version: int = 1,
    max_attempts: int = 3,
) -> dict[str, Any]:
    for text in (job_key, job_type, policy_version):
        if not isinstance(text, str) or not 1 <= len(text) <= 250:
            raise InvalidInputError("Job keys, type and policy must be nonempty bounded strings.")
    if type(schema_version) is not int or schema_version != 1:
        raise InvalidInputError("Unsupported job schema version.")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 100:
        raise InvalidInputError("Job attempts must be bounded between 1 and 100.")
    _scoped_reference(connection, context, "evidence_events", event_id)
    _scoped_reference(connection, context, "artifacts", payload_id)
    connection.execute(
        """INSERT INTO outbox_jobs
        (workspace_id,id,job_key,job_type,event_id,payload_id,policy_version,schema_version,max_attempts)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (workspace_id,job_key) DO NOTHING""",
        (
            context.workspace_id,
            uuid4(),
            job_key,
            job_type,
            event_id,
            payload_id,
            policy_version,
            schema_version,
            max_attempts,
        ),
    )
    row = connection.execute(
        "SELECT * FROM outbox_jobs WHERE workspace_id=%s AND job_key=%s",
        (context.workspace_id, job_key),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError("Resource is unavailable in this workspace.")
    expected = (job_type, event_id, payload_id, policy_version, schema_version, max_attempts)
    actual = tuple(
        row[key]
        for key in (
            "job_type",
            "event_id",
            "payload_id",
            "policy_version",
            "schema_version",
            "max_attempts",
        )
    )
    if actual != expected:
        raise RevisionConflictError("The job key was already used for different work.")
    return dict(row)


@dataclass(frozen=True, slots=True)
class JobLease:
    workspace_id: UUID
    job_id: UUID
    generation: int
    worker_id: str
    lease_until: datetime
    job_type: str
    payload_id: UUID | None
    event_id: UUID
    policy_version: str
    attempt: int


def _seconds(value: float, maximum: float) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise InvalidInputError("Lease and retry durations must be finite numbers.")
    if not 0 < value <= maximum:
        raise InvalidInputError("Lease or retry duration is outside the supported range.")


class Outbox:
    def __init__(self, db: Database) -> None:
        self.db = db

    def publish(
        self,
        context: WorkspaceContext,
        *,
        key: str,
        event_type: str,
        details: dict[str, Any],
        job_type: str = "fixture",
        payload_id: UUID | None = None,
        episode_id: UUID | None = None,
        policy_version: str = "fixture-v1",
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        """Commit one event and job together, with replay-safe command identity."""
        from potbot.persistence.database import idempotent_command

        request = {
            "event_type": event_type,
            "details": details,
            "job_type": job_type,
            "payload_id": str(payload_id) if payload_id else None,
            "episode_id": str(episode_id) if episode_id else None,
            "policy_version": policy_version,
            "max_attempts": max_attempts,
        }
        with self.db.transaction(context) as connection:

            def command() -> dict[str, Any]:
                event = append_event(
                    connection,
                    context,
                    event_type,
                    details=details,
                    payload_id=payload_id,
                    episode_id=episode_id,
                )
                job = enqueue_job(
                    connection,
                    context,
                    job_key=key,
                    job_type=job_type,
                    event_id=event["id"],
                    payload_id=payload_id,
                    policy_version=policy_version,
                    max_attempts=max_attempts,
                )
                return {"event_id": str(event["id"]), "job_id": str(job["id"])}

            return idempotent_command(connection, context, "publish_job", key, request, command)

    def claim(
        self, context: WorkspaceContext, worker_id: str, *, lease_seconds: float = 30
    ) -> JobLease | None:
        _seconds(lease_seconds, 3600)
        if not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 200:
            raise InvalidInputError("Worker identity must be a nonempty bounded string.")
        with self.db.transaction(context) as connection:
            connection.execute(
                """UPDATE outbox_jobs SET status='failed',lease_until=NULL,lease_owner=NULL,
                last_error_code='attempts_exhausted',updated_at=clock_timestamp()
                WHERE workspace_id=%s AND status='running' AND lease_until<=clock_timestamp()
                AND attempts>=max_attempts""",
                (context.workspace_id,),
            )
            row = connection.execute(
                """SELECT * FROM outbox_jobs WHERE workspace_id=%s AND attempts<max_attempts
                AND ((status='pending' AND available_at<=clock_timestamp())
                    OR (status='running' AND lease_until<=clock_timestamp()))
                ORDER BY available_at,created_at,id FOR UPDATE SKIP LOCKED LIMIT 1""",
                (context.workspace_id,),
            ).fetchone()
            if row is None:
                return None
            row = connection.execute(
                """UPDATE outbox_jobs SET status='running',generation=generation+1,
                attempts=attempts+1,lease_owner=%s,
                lease_until=clock_timestamp()+(%s * interval '1 second'),
                updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s RETURNING *""",
                (worker_id, lease_seconds, context.workspace_id, row["id"]),
            ).fetchone()
            return JobLease(
                context.workspace_id,
                row["id"],
                row["generation"],
                worker_id,
                row["lease_until"],
                row["job_type"],
                row["payload_id"],
                row["event_id"],
                row["policy_version"],
                row["attempts"],
            )

    @staticmethod
    def _locked(connection: Any, context: WorkspaceContext, lease: JobLease) -> dict[str, Any]:
        if context.workspace_id != lease.workspace_id:
            raise ScopeDeniedError("Resource is unavailable in this workspace.")
        row = connection.execute(
            """SELECT *, lease_until>clock_timestamp() AS valid_lease FROM outbox_jobs
            WHERE workspace_id=%s AND id=%s FOR UPDATE""",
            (context.workspace_id, lease.job_id),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("Resource is unavailable in this workspace.")
        if (
            row["generation"] != lease.generation
            or row["lease_owner"] != lease.worker_id
            or row["status"] != "running"
            or not row["valid_lease"]
        ):
            raise RevisionConflictError("The worker lease is no longer current.")
        return dict(row)

    def renew(
        self, context: WorkspaceContext, lease: JobLease, *, lease_seconds: float = 30
    ) -> datetime:
        _seconds(lease_seconds, 3600)
        with self.db.transaction(context) as connection:
            self._locked(connection, context, lease)
            row = connection.execute(
                """UPDATE outbox_jobs SET lease_until=clock_timestamp()+(%s * interval '1 second'),
                updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s
                AND generation=%s AND lease_owner=%s AND status='running'
                AND lease_until>clock_timestamp() RETURNING lease_until""",
                (
                    lease_seconds,
                    context.workspace_id,
                    lease.job_id,
                    lease.generation,
                    lease.worker_id,
                ),
            ).fetchone()
            if row is None:
                raise RevisionConflictError("The worker lease is no longer current.")
            deadline: datetime = row["lease_until"]
            return deadline

    def _complete(
        self, connection: Any, context: WorkspaceContext, lease: JobLease, artifact_id: UUID
    ) -> dict[str, Any]:
        _scoped_reference(connection, context, "artifacts", artifact_id)
        row = connection.execute(
            """UPDATE outbox_jobs SET status='completed',completion_artifact_id=%s,
            lease_until=NULL,lease_owner=NULL,updated_at=clock_timestamp()
            WHERE workspace_id=%s AND id=%s AND generation=%s AND lease_owner=%s
            AND status='running' AND lease_until>clock_timestamp() RETURNING *""",
            (artifact_id, context.workspace_id, lease.job_id, lease.generation, lease.worker_id),
        ).fetchone()
        if row is None:
            raise RevisionConflictError("The worker lease is no longer current.")
        return dict(row)

    def complete(
        self, context: WorkspaceContext, lease: JobLease, artifact_id: UUID
    ) -> dict[str, Any]:
        with self.db.transaction(context) as connection:
            self._locked(connection, context, lease)
            return self._complete(connection, context, lease, artifact_id)

    def complete_payload(
        self,
        context: WorkspaceContext,
        lease: JobLease,
        artifacts: ArtifactStore,
        data: bytes,
        media_type: str = "application/json",
    ) -> dict[str, Any]:
        with self.db.transaction(context) as connection:
            self._locked(connection, context, lease)
            artifact = artifacts.put_in_transaction(connection, context, data, media_type)
            return self._complete(connection, context, lease, artifact["id"])

    def fail(
        self,
        context: WorkspaceContext,
        lease: JobLease,
        *,
        error_code: str,
        retryable: bool,
        retry_after_seconds: float = 1,
    ) -> dict[str, Any]:
        _seconds(retry_after_seconds, 86400)
        allowed = {"source_unavailable", "model_unavailable", "timeout", "tool_failure"}
        if not isinstance(error_code, str) or not 1 <= len(error_code) <= 100:
            raise InvalidInputError("Worker errors require a classified code.")
        if type(retryable) is not bool or (retryable and error_code not in allowed):
            raise InvalidInputError("This failure does not permit an automatic retry.")
        with self.db.transaction(context) as connection:
            old = self._locked(connection, context, lease)
            status = "pending" if retryable and old["attempts"] < old["max_attempts"] else "failed"
            row = connection.execute(
                """UPDATE outbox_jobs SET status=%s,last_error_code=%s,
                lease_until=NULL,lease_owner=NULL,
                available_at=clock_timestamp()+(%s * interval '1 second'),
                updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s
                AND generation=%s AND lease_owner=%s AND status='running'
                AND lease_until>clock_timestamp() RETURNING *""",
                (
                    status,
                    error_code,
                    retry_after_seconds,
                    context.workspace_id,
                    lease.job_id,
                    lease.generation,
                    lease.worker_id,
                ),
            ).fetchone()
            if row is None:
                raise RevisionConflictError("The worker lease is no longer current.")
            return dict(row)

    def inspect(self, context: WorkspaceContext, job_id: UUID) -> dict[str, Any]:
        with self.db.transaction(context) as connection:
            row = connection.execute(
                "SELECT * FROM outbox_jobs WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, job_id),
            ).fetchone()
            if row is None:
                raise ScopeDeniedError("Resource is unavailable in this workspace.")
            return dict(row)


class FixtureWorker:
    """Harmless deterministic handler, suitable for crash/restart demonstrations."""

    def __init__(self, outbox: Outbox, artifacts: ArtifactStore, worker_id: str) -> None:
        self.outbox = outbox
        self.artifacts = artifacts
        self.worker_id = worker_id

    def run_once(
        self, context: WorkspaceContext, *, lease_seconds: float = 30
    ) -> dict[str, Any] | None:
        lease = self.outbox.claim(context, self.worker_id, lease_seconds=lease_seconds)
        if lease is None:
            return None
        if lease.job_type != "fixture" or lease.policy_version != "fixture-v1":
            return self.outbox.fail(context, lease, error_code="unsupported_job", retryable=False)
        # Work happens outside the claim transaction. Content IDs make its
        # artifact publication idempotent; the lease fences the final SQL write.
        payload_digest = None
        if lease.payload_id:
            import hashlib

            from potbot.persistence.artifacts import ArtifactIntegrityError

            try:
                payload_digest = hashlib.sha256(
                    self.artifacts.read(context, lease.payload_id)
                ).hexdigest()
            except ArtifactIntegrityError:
                return self.outbox.fail(
                    context, lease, error_code="artifact_integrity", retryable=False
                )
        result = canonical_json(
            {
                "schema_version": 1,
                "job_id": str(lease.job_id),
                "event_id": str(lease.event_id),
                "payload_digest": payload_digest,
                "handler": "fixture-v1",
            }
        ).encode()
        return self.outbox.complete_payload(context, lease, self.artifacts, result)
