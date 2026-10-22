"""Phase 7 incremental *local-only* authenticated research execution.

The caller must already hold an authenticated, restricted PostgreSQL workspace
context. Untrusted model output cannot create a lease or a source scope. Queue,
claim, snapshot inspection and publication are separate, restartable steps;
expired leases are quarantined, not replayed. No HTTP or code execution occurs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.artifacts import ArtifactStore
from potbot.persistence.database import Database, idempotent_command
from potbot.persistence.evidence import append_event
from potbot.research.contracts import ResearchAction, ResearchCategory, ResearchObservation
from potbot.research.repository import SnapshotResearchAdapter
from potbot.semantics.releases import read_task_pin
from potbot.tools.repository_snapshot import RepositorySnapshot

_WORKER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}\Z")
_MAX_RESULTS = 100


def _action(
    conn: Any, context: WorkspaceContext, action_id: UUID
) -> tuple[ResearchAction, dict[str, Any]]:
    row = conn.execute(
        "SELECT a.content,a.payload_digest,a.identity_digest,a.episode_id,"
        "s.repository_id,s.snapshot_artifact_id,s.content_digest,s.manifest "
        "FROM research_actions a JOIN repository_states s ON "
        "s.workspace_id=a.workspace_id AND s.id=a.repository_state_id "
        "WHERE a.workspace_id=%s AND a.id=%s",
        (context.workspace_id, action_id),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError("Research action or pinned repository state is unavailable")
    if content_hash(row["content"]) != row["payload_digest"]:
        raise StateMismatchError("Research action payload was modified")
    action = ResearchAction.from_dict(row["content"])
    if (
        action.identity != row["identity_digest"]
        or action.action_id != action_id
        or action.scope.workspace_id != context.workspace_id
        or action.scope.task_id != row["episode_id"]
    ):
        raise StateMismatchError("Research action identity or workspace changed")
    return action, dict(row)


def _current(
    conn: Any, context: WorkspaceContext, action: ResearchAction, snapshot_row: dict[str, Any]
) -> None:
    """Recheck exactly the active need, task revision and sealed release.

    Row locks serialize against lifecycle/need revision changes on publication.
    """
    scope = action.scope
    episode = conn.execute(
        "SELECT active_contract_id,status,repository_id FROM episodes "
        "WHERE workspace_id=%s AND id=%s FOR SHARE",
        (context.workspace_id, scope.task_id),
    ).fetchone()
    need = conn.execute(
        "SELECT n.episode_id,s.current_revision_id,s.status FROM information_needs n "
        "JOIN need_status s ON s.workspace_id=n.workspace_id AND s.need_id=n.id "
        "WHERE n.workspace_id=%s AND n.id=%s FOR SHARE OF n,s",
        (context.workspace_id, scope.need_id),
    ).fetchone()
    if (
        episode is None
        or need is None
        or episode["status"] != "running"
        or episode["active_contract_id"] != scope.contract_revision_id
        or episode["repository_id"] != snapshot_row["repository_id"]
        or need["episode_id"] != scope.task_id
        or need["status"] != "open"
        or need["current_revision_id"] != scope.need_revision_id
    ):
        raise RevisionConflictError("Research action is no longer current for its task/need")
    pin = read_task_pin(conn, context, scope.task_id, required=True)
    assert pin is not None
    if pin["content"]["release_digest"] != scope.semantic_release_digest:
        raise RevisionConflictError("Research action semantic release pin changed")


def _run(
    conn: Any, context: WorkspaceContext, run_id: UUID, *, lock: bool = False
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM research_runs WHERE workspace_id=%s AND id=%s"
        + (" FOR UPDATE" if lock else ""),
        (context.workspace_id, run_id),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError("Research execution is not available in this workspace")
    return dict(row)


@dataclass(frozen=True, slots=True)
class ResearchLease:
    workspace_id: UUID
    run_id: UUID
    action_id: UUID
    source_artifact_id: UUID
    source_revision: str
    action_identity: str
    worker_id: str
    generation: int
    expires_at: datetime

    def __post_init__(self) -> None:
        for name in ("workspace_id", "run_id", "action_id", "source_artifact_id"):
            if not isinstance(getattr(self, name), UUID):
                raise InvalidInputError("Research lease requires UUID identities")
        if not isinstance(self.worker_id, str) or not _WORKER.fullmatch(self.worker_id):
            raise InvalidInputError("Research lease requires a literal worker identity")
        if type(self.generation) is not int or self.generation < 1:
            raise InvalidInputError("Research lease generation must be positive")
        if not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None:
            raise InvalidInputError("Research lease deadline must be timezone aware")
        for digest in (self.source_revision, self.action_identity):
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise InvalidInputError("Research lease content digest is invalid")


class ResearchRunStore:
    """Durable exact-snapshot inspector queue with one fenced attempt per action."""

    def __init__(self, database: Database, artifacts: ArtifactStore) -> None:
        if artifacts.db != database:
            raise InvalidInputError("Research execution and artifact storage must share a database")
        self.database, self.artifacts = database, artifacts

    def queue(self, context: WorkspaceContext, action_id: UUID, key: str) -> dict[str, Any]:
        if not isinstance(action_id, UUID):
            raise InvalidInputError("Research queue requires an action UUID")
        with self.database.transaction(context) as conn:

            def create() -> dict[str, Any]:
                # Different client idempotency keys still serialize on one action.
                conn.execute(
                    "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s,0))",
                    (f"{context.workspace_id}:research-queue:{action_id}",),
                )
                action, snapshot_row = _action(conn, context, action_id)
                if action.query.category != ResearchCategory.REPOSITORY:
                    raise CapabilityUnavailableError(
                        "External sources need a separately approved egress/budgeted worker"
                    )
                _current(conn, context, action, snapshot_row)
                prior = conn.execute(
                    "SELECT id,action_identity,source_revision,source_artifact_id "
                    "FROM research_runs WHERE workspace_id=%s AND action_id=%s",
                    (context.workspace_id, action_id),
                ).fetchone()
                if prior is not None:
                    if (
                        prior["action_identity"] != action.identity
                        or prior["source_revision"] != snapshot_row["content_digest"]
                        or prior["source_artifact_id"] != snapshot_row["snapshot_artifact_id"]
                    ):
                        raise StateMismatchError(
                            "Research queue identity conflicts with stored run"
                        )
                    return {"run_id": str(prior["id"]), "reused": True}
                identity = uuid4()
                conn.execute(
                    "INSERT INTO research_runs(workspace_id,id,episode_id,action_id,"
                    "source_artifact_id,action_identity,source_revision) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        identity,
                        action.scope.task_id,
                        action.action_id,
                        snapshot_row["snapshot_artifact_id"],
                        action.identity,
                        snapshot_row["content_digest"],
                    ),
                )
                return {"run_id": str(identity), "reused": False}

            return idempotent_command(
                conn, context, "research_queue_local", key, {"action_id": str(action_id)}, create
            )

    def claim(
        self, context: WorkspaceContext, worker_id: str, *, lease_seconds: int = 90
    ) -> ResearchLease | None:
        if not isinstance(worker_id, str) or not _WORKER.fullmatch(worker_id):
            raise InvalidInputError("Worker identity must be literal and bounded")
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 900:
            raise InvalidInputError("Research lease duration must be from 5 to 900 seconds")
        with self.database.transaction(context) as conn:
            row = conn.execute(
                "SELECT * FROM research_runs WHERE workspace_id=%s AND state='queued' "
                "ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1",
                (context.workspace_id,),
            ).fetchone()
            if row is None:
                return None
            action, snapshot_row = _action(conn, context, row["action_id"])
            try:
                _current(conn, context, action, snapshot_row)
            except (RevisionConflictError, ScopeDeniedError, CapabilityUnavailableError):
                # Never run a queued request after its task/need changes.
                conn.execute(
                    "UPDATE research_runs SET state='blocked',error_code='stale_scope',"
                    "finished_at=clock_timestamp(),updated_at=clock_timestamp() "
                    "WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, row["id"]),
                )
                return None
            if (
                action.query.category != ResearchCategory.REPOSITORY
                or row["action_identity"] != action.identity
                or row["source_revision"] != snapshot_row["content_digest"]
                or row["source_artifact_id"] != snapshot_row["snapshot_artifact_id"]
            ):
                raise StateMismatchError("Queued source no longer matches the admitted action")
            claimed = conn.execute(
                "UPDATE research_runs SET state='running',generation=generation+1,"
                "lease_owner=%s,lease_until=clock_timestamp()+make_interval(secs=>%s),"
                "started_at=clock_timestamp(),updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s RETURNING *",
                (worker_id, lease_seconds, context.workspace_id, row["id"]),
            ).fetchone()
            assert claimed is not None
            return ResearchLease(
                context.workspace_id,
                claimed["id"],
                action.action_id,
                claimed["source_artifact_id"],
                claimed["source_revision"],
                claimed["action_identity"],
                worker_id,
                claimed["generation"],
                claimed["lease_until"],
            )

    @staticmethod
    def _check_lease(conn: Any, context: WorkspaceContext, lease: ResearchLease) -> dict[str, Any]:
        if not isinstance(lease, ResearchLease) or lease.workspace_id != context.workspace_id:
            raise ScopeDeniedError("Research lease is not in the authenticated workspace")
        run = _run(conn, context, lease.run_id, lock=True)
        valid = (
            run["state"] == "running"
            and run["action_id"] == lease.action_id
            and run["source_artifact_id"] == lease.source_artifact_id
            and run["source_revision"] == lease.source_revision
            and run["action_identity"] == lease.action_identity
            and run["lease_owner"] == lease.worker_id
            and run["generation"] == lease.generation
            and run["lease_until"] == lease.expires_at
        )
        if not valid:
            raise RevisionConflictError("Research lease lost or superseded")
        still_valid = conn.execute(
            "SELECT clock_timestamp() < %s AS valid", (lease.expires_at,)
        ).fetchone()
        if still_valid is None or not still_valid["valid"]:
            raise RevisionConflictError("Research lease expired before publication")
        return run

    def complete(
        self,
        context: WorkspaceContext,
        lease: ResearchLease,
        observations: tuple[ResearchObservation, ...],
    ) -> dict[str, Any]:
        """Publish exact reproduced observations, bytes, event and receipt atomically.

        A caller-supplied excerpt, even with the right file digest, is never
        trusted. Re-inspect the immutable artifact under the publication lock.
        """
        if (
            not isinstance(observations, tuple)
            or not 1 <= len(observations) <= _MAX_RESULTS
            or any(not isinstance(item, ResearchObservation) for item in observations)
        ):
            raise InvalidInputError("Research completion needs 1..100 typed observations")
        from psycopg.types.json import Jsonb

        with self.database.transaction(context) as conn:
            run = self._check_lease(conn, context, lease)
            action, snapshot_row = _action(conn, context, run["action_id"])
            _current(conn, context, action, snapshot_row)
            if (
                snapshot_row["snapshot_artifact_id"] != lease.source_artifact_id
                or snapshot_row["content_digest"] != lease.source_revision
                or action.identity != lease.action_identity
            ):
                raise RevisionConflictError("Research source or action changed before completion")
            source_bytes = self.artifacts.read_in_transaction(
                conn, context, lease.source_artifact_id
            )
            snapshot = RepositorySnapshot(source_bytes)
            if (
                snapshot.content_digest != lease.source_revision
                or snapshot.manifest != snapshot_row["manifest"]
            ):
                raise StateMismatchError("Repository snapshot provenance is invalid")
            reader = SnapshotResearchAdapter(
                snapshot,
                repository_state_id=action.scope.repository_state_id,
                workspace_id=context.workspace_id,
                task_id=action.scope.task_id,
            )
            inspected = reader.inspect(action, now=observations[0].retrieved_at)
            if inspected != observations:
                raise StateMismatchError(
                    "Research observations do not match inspected source bytes"
                )
            payload = {
                "schema_version": 1,
                "primary_type_ref": "synny://type/potbot-research-observation-batch",
                "run_id": str(lease.run_id),
                "action_id": str(lease.action_id),
                "action_identity": action.identity,
                "source_artifact_id": str(lease.source_artifact_id),
                "source_revision": lease.source_revision,
                "observations": [item.to_dict() for item in observations],
            }
            output = self.artifacts.put_in_transaction(
                conn,
                context,
                canonical_json(payload).encode("utf-8"),
                "application/vnd.potbot.research-observation-batch+json",
            )
            inserted: list[str] = []
            for item in observations:
                identity = uuid4()
                conn.execute(
                    "INSERT INTO research_observations(workspace_id,id,episode_id,"
                    "action_id,source_artifact_id,observation_digest,status,content) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        identity,
                        action.scope.task_id,
                        action.action_id,
                        lease.source_artifact_id,
                        item.fingerprint,
                        item.status.value,
                        Jsonb(item.to_dict()),
                    ),
                )
                inserted.append(str(identity))
            receipt = {
                "schema_version": 1,
                "run_id": str(lease.run_id),
                "action_id": str(lease.action_id),
                "action_identity": action.identity,
                "workspace_id": str(context.workspace_id),
                "task_id": str(action.scope.task_id),
                "contract_revision_id": str(action.scope.contract_revision_id),
                "need_revision_id": str(action.scope.need_revision_id),
                "repository_state_id": str(action.scope.repository_state_id),
                "semantic_release_digest": action.scope.semantic_release_digest,
                "source_snapshot_artifact_id": str(lease.source_artifact_id),
                "source_revision": lease.source_revision,
                "output_artifact_id": str(output["id"]),
                "output_sha256": output["digest"],
                "observation_fingerprints": [item.fingerprint for item in observations],
                "observation_ids": inserted,
                "lease_generation": lease.generation,
                "status": "source_inspection_completed",
                "cog_activation": "pending_pinned_syn_registration",
                "execution_authorized": False,
                "need_resolved": False,
                "license_assessment": "unknown",
            }
            digest = content_hash(receipt)
            conn.execute(
                "INSERT INTO research_run_receipts(workspace_id,id,episode_id,run_id,"
                "result_artifact_id,receipt_digest,content) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    context.workspace_id,
                    uuid4(),
                    action.scope.task_id,
                    lease.run_id,
                    output["id"],
                    digest,
                    Jsonb(receipt),
                ),
            )
            append_event(
                conn,
                context,
                "repository_research_observed",
                payload_id=output["id"],
                episode_id=action.scope.task_id,
                producer_kind="tool",
                producer_id="potbot.repository_research",
                details={
                    "research_run_id": str(lease.run_id),
                    "research_action_id": str(lease.action_id),
                    "receipt_digest": digest,
                    "source_revision": lease.source_revision,
                },
            )
            conn.execute(
                "UPDATE research_runs SET state='succeeded',lease_owner=NULL,lease_until=NULL,"
                "result_artifact_id=%s,receipt_digest=%s,finished_at=clock_timestamp(),"
                "updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s",
                (output["id"], digest, context.workspace_id, lease.run_id),
            )
            return {
                "run_id": str(lease.run_id),
                "state": "succeeded",
                "result_artifact_id": str(output["id"]),
                "receipt_digest": digest,
                "observation_count": len(observations),
                "need_resolved": False,
            }

    def block(
        self, context: WorkspaceContext, lease: ResearchLease, error_code: str
    ) -> dict[str, Any]:
        if not isinstance(error_code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,60}", error_code):
            raise InvalidInputError("Research terminal error requires a safe code")
        with self.database.transaction(context) as conn:
            self._check_lease(conn, context, lease)
            conn.execute(
                "UPDATE research_runs SET state='blocked',error_code=%s,"
                "lease_owner=NULL,lease_until=NULL,finished_at=clock_timestamp(),"
                "updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s",
                (error_code, context.workspace_id, lease.run_id),
            )
            return {"run_id": str(lease.run_id), "state": "blocked", "error_code": error_code}

    def quarantine_expired(self, context: WorkspaceContext) -> int:
        """No automatic retry, even though local reads themselves are pure."""
        with self.database.transaction(context) as conn:
            rows = conn.execute(
                "UPDATE research_runs SET state='indeterminate',error_code='lease_expired',"
                "lease_owner=NULL,lease_until=NULL,finished_at=clock_timestamp(),"
                "updated_at=clock_timestamp() WHERE workspace_id=%s AND state='running' "
                "AND lease_until<=clock_timestamp() RETURNING id",
                (context.workspace_id,),
            ).fetchall()
            return len(rows)

    def inspect(self, context: WorkspaceContext, run_id: UUID) -> dict[str, Any]:
        if not isinstance(run_id, UUID):
            raise InvalidInputError("Research run requires a UUID")
        with self.database.transaction(context) as conn:
            row = _run(conn, context, run_id)
            result = {
                "run_id": str(row["id"]),
                "action_id": str(row["action_id"]),
                "state": row["state"],
                "generation": row["generation"],
                "source_revision": row["source_revision"],
                "result_artifact_id": str(row["result_artifact_id"])
                if row["result_artifact_id"]
                else None,
                "receipt_digest": row["receipt_digest"],
                "error_code": row["error_code"],
            }
            if row["state"] == "succeeded":
                receipt = conn.execute(
                    "SELECT content,receipt_digest FROM research_run_receipts "
                    "WHERE workspace_id=%s AND run_id=%s",
                    (context.workspace_id, run_id),
                ).fetchone()
                if receipt is None or content_hash(receipt["content"]) != receipt["receipt_digest"]:
                    raise StateMismatchError(
                        "Research execution receipt is unavailable or modified"
                    )
                if receipt["receipt_digest"] != row["receipt_digest"]:
                    raise StateMismatchError("Research execution receipt identity differs")
                result["receipt"] = receipt["content"]
            return result


class LocalResearchWorker:
    """One bounded repository action, never a network fetch or model call."""

    def __init__(self, store: ResearchRunStore) -> None:
        self.store = store

    def run_once(self, context: WorkspaceContext, worker_id: str) -> dict[str, Any] | None:
        self.store.quarantine_expired(context)
        lease = self.store.claim(context, worker_id)
        if lease is None:
            return None
        try:
            from potbot.controller.research_store import ResearchStore

            action = ResearchAction.from_dict(
                ResearchStore(self.store.database).inspect(context, lease.action_id)
            )
            raw = self.store.artifacts.read(context, lease.source_artifact_id)
            snapshot = RepositorySnapshot(raw)
            if snapshot.content_digest != lease.source_revision:
                raise StateMismatchError("Snapshot digest differs from the leased identity")
            adapter = SnapshotResearchAdapter(
                snapshot,
                repository_state_id=action.scope.repository_state_id,
                workspace_id=context.workspace_id,
                task_id=action.scope.task_id,
            )
            return self.store.complete(context, lease, adapter.inspect(action))
        except RevisionConflictError:
            # A stale/lost lease must never be rewritten as a successful result.
            raise
        except Exception as error:
            code = getattr(getattr(error, "code", None), "value", "internal_error")
            try:
                return self.store.block(context, lease, code)
            except RevisionConflictError:
                # An expired worker cannot revise an already quarantined run.
                raise
