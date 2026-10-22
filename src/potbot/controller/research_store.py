"""Workspace-scoped, replay-safe publication of Phase 7 research records.

Actions remain inert declarations. Only the separately authorized research
executor can fetch or inspect sources and publish actual observations.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    InvalidInputError, RevisionConflictError, ScopeDeniedError, StateMismatchError,
)
from potbot.domain.serialization import content_hash
from potbot.persistence.database import Database, idempotent_command
from potbot.research.contracts import ResearchAction, ResearchObservation


class ResearchStore:
    def __init__(self, database: Database):
        self.database = database

    def admit(self, context: WorkspaceContext, action: ResearchAction,
              idempotency_key: str) -> dict[str, Any]:
        if not isinstance(action, ResearchAction):
            raise InvalidInputError("research admission requires a typed action")
        if context.workspace_id != action.scope.workspace_id:
            raise ScopeDeniedError("research action workspace is not accessible")
        request = action.to_dict()
        with self.database.transaction(context) as conn:
            def write() -> dict[str, Any]:
                scope, q = action.scope, action.query
                episode = conn.execute(
                    "SELECT active_contract_id,repository_id,status FROM episodes "
                    "WHERE workspace_id=%s AND id=%s FOR SHARE",
                    (context.workspace_id, scope.task_id),
                ).fetchone()
                need = conn.execute(
                    "SELECT n.episode_id,s.current_revision_id,s.status FROM information_needs n "
                    "JOIN need_status s ON s.workspace_id=n.workspace_id AND s.need_id=n.id "
                    "WHERE n.workspace_id=%s AND n.id=%s FOR SHARE OF s,n",
                    (context.workspace_id, scope.need_id),
                ).fetchone()
                state = conn.execute(
                    "SELECT repository_id FROM repository_states "
                    "WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, scope.repository_state_id),
                ).fetchone()
                pin = conn.execute(
                    "SELECT r.content_hash FROM task_semantic_pins p "
                    "JOIN semantic_releases r ON r.workspace_id=p.workspace_id "
                    "AND r.id=p.release_id WHERE p.workspace_id=%s AND p.episode_id=%s",
                    (context.workspace_id, scope.task_id),
                ).fetchone()
                if (episode is None or need is None or state is None or pin is None
                        or need["episode_id"] != scope.task_id
                        or state["repository_id"] != episode["repository_id"]):
                    raise ScopeDeniedError("research scope is unavailable")
                if (episode["status"] != "running"
                        or episode["active_contract_id"] != scope.contract_revision_id
                        or need["status"] != "open"
                        or need["current_revision_id"] != scope.need_revision_id
                        or pin["content_hash"] != scope.semantic_release_digest):
                    raise RevisionConflictError("research need or task revision is no longer current")
                if action.predecessor_id is not None:
                    previous = conn.execute(
                        "SELECT content FROM research_actions WHERE workspace_id=%s "
                        "AND episode_id=%s AND id=%s",
                        (context.workspace_id, scope.task_id, action.predecessor_id),
                    ).fetchone()
                    if previous is None:
                        raise RevisionConflictError("research predecessor is unavailable")
                    old = ResearchAction.from_dict(previous["content"])
                    if (old.scope != action.scope or old.query.category != q.category
                            or old.query.locator != q.locator
                            or old.query.target_version != q.target_version
                            or not set(old.query.required_identifiers).issubset(q.required_identifiers)
                            or old.query.search_text == q.search_text
                            or any(token not in q.search_text for token in q.required_identifiers)):
                        raise RevisionConflictError("research query lineage is incompatible")
                elif any(token not in q.search_text for token in q.required_identifiers):
                    raise InvalidInputError("research query dropped a required identifier")
                existing = conn.execute(
                    "SELECT id FROM research_actions WHERE workspace_id=%s "
                    "AND episode_id=%s AND identity_digest=%s",
                    (context.workspace_id, scope.task_id, action.identity),
                ).fetchone()
                if existing is not None:
                    return {"action_id": str(existing["id"]),
                            "identity": action.identity, "reused": True}
                from psycopg.types.json import Jsonb

                conn.execute(
                    "INSERT INTO research_actions(workspace_id,id,episode_id,need_id,"
                    "need_revision_id,contract_revision_id,repository_state_id,"
                    "predecessor_id,identity_digest,payload_digest,source_category,"
                    "operation,content) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (context.workspace_id, action.action_id, scope.task_id,
                     scope.need_id, scope.need_revision_id, scope.contract_revision_id,
                     scope.repository_state_id, action.predecessor_id,
                     action.identity, content_hash(request),
                     q.category.value, q.operation.value, Jsonb(request)),
                )
                return {"action_id": str(action.action_id),
                        "identity": action.identity, "reused": False}
            return idempotent_command(
                conn, context, "phase7_research_action", idempotency_key, request, write,
            )

    def inspect(self, context: WorkspaceContext, action_id: UUID) -> dict[str, Any]:
        if not isinstance(action_id, UUID):
            raise InvalidInputError("research action ID must be a UUID")
        with self.database.transaction(context) as conn:
            row = conn.execute(
                "SELECT content,payload_digest FROM research_actions WHERE "
                "workspace_id=%s AND id=%s",
                (context.workspace_id, action_id),
            ).fetchone()
            if row is None:
                raise ScopeDeniedError("research action is unavailable")
            if content_hash(row["content"]) != row["payload_digest"]:
                raise StateMismatchError("retained research action has changed")
            return ResearchAction.from_dict(row["content"]).to_dict()

    def record_snapshot_observation(
        self, context: WorkspaceContext, observation: ResearchObservation,
        source_snapshot_artifact_id: UUID, idempotency_key: str,
    ) -> dict[str, Any]:
        """Record an adapter observation against the immutable repository artifact.

        The actual content is inspected by SnapshotResearchAdapter, which must
        be called by an authenticated worker, not a public JSON endpoint.
        """
        if not isinstance(observation, ResearchObservation) or not isinstance(
            source_snapshot_artifact_id, UUID
        ):
            raise InvalidInputError("a typed observation and snapshot artifact are required")
        request = {
            "observation": observation.to_dict(),
            "source_snapshot_artifact_id": str(source_snapshot_artifact_id),
        }
        with self.database.transaction(context) as conn:
            def write() -> dict[str, Any]:
                record = conn.execute(
                    "SELECT a.episode_id,a.repository_state_id,a.content,"
                    "s.content_digest,s.snapshot_artifact_id,s.manifest "
                    "FROM research_actions a JOIN repository_states s ON "
                    "s.workspace_id=a.workspace_id AND s.id=a.repository_state_id "
                    "WHERE a.workspace_id=%s AND a.id=%s",
                    (context.workspace_id, observation.action_id),
                ).fetchone()
                if record is None:
                    raise ScopeDeniedError("observation source action is unavailable")
                action = ResearchAction.from_dict(record["content"])
                if (action.query.category.value != "repository"
                        or record["content_digest"] != observation.source_revision
                        or record["snapshot_artifact_id"] != source_snapshot_artifact_id):
                    raise RevisionConflictError("observation references a different snapshot")
                files = record["manifest"].get("files", [])
                if observation.content_digest is not None:
                    matches = [
                        entry for entry in files if
                        entry.get("path") == observation.resolved_locator
                        and entry.get("sha256") == observation.content_digest
                        and entry.get("kind") == "file"
                    ]
                    if not matches:
                        raise RevisionConflictError("observed source hash is not in the snapshot")
                from psycopg.types.json import Jsonb

                existing = conn.execute(
                    "SELECT id FROM research_observations WHERE workspace_id=%s "
                    "AND episode_id=%s AND action_id=%s AND observation_digest=%s",
                    (context.workspace_id, record["episode_id"], observation.action_id,
                     observation.fingerprint),
                ).fetchone()
                if existing is not None:
                    return {"observation_id": str(existing["id"]), "reused": True,
                            "fingerprint": observation.fingerprint}
                identity = uuid4()
                conn.execute(
                    "INSERT INTO research_observations(workspace_id,id,episode_id,"
                    "action_id,source_artifact_id,observation_digest,status,content) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (context.workspace_id, identity, record["episode_id"],
                     observation.action_id, source_snapshot_artifact_id,
                     observation.fingerprint, observation.status.value,
                     Jsonb(observation.to_dict())),
                )
                return {"observation_id": str(identity), "reused": False,
                        "fingerprint": observation.fingerprint}
            return idempotent_command(
                conn, context, "phase7_research_observation", idempotency_key,
                request, write,
            )
