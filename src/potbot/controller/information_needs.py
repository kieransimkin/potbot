"""Durable questions and their immutable, scoped revision history."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from potbot.controller.tasks import json_record, lock_episode
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.domain.execution import bounded_strings
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.tasks import validate_amounts
from potbot.persistence.database import Database, idempotent_command
from potbot.persistence.evidence import append_event


@dataclass(frozen=True, slots=True)
class NeedSpec:
    question: str
    pending_decision: str
    alternatives: tuple[str, ...]
    required_evidence: tuple[str, ...]
    resolution_criteria: tuple[str, ...]
    scope: str
    known_evidence: tuple[UUID, ...] = ()
    priority: int = 50
    budget: tuple[tuple[str, int], ...] = ()
    query_profiles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in ("question", "pending_decision", "scope"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip() or len(value) > 8000:
                raise InvalidInputError(f"{field} must be explicit and bounded.")
        for field in ("alternatives", "required_evidence", "resolution_criteria", "query_profiles"):
            bounded_strings(getattr(self, field), field)
        if not self.required_evidence or not self.resolution_criteria:
            raise InvalidInputError("A need requires evidence and resolution criteria.")
        if type(self.priority) is not int or not 0 <= self.priority <= 100:
            raise InvalidInputError("Need priority must be between zero and 100.")
        if (
            not isinstance(self.known_evidence, tuple)
            or len(self.known_evidence) > 128
            or any(not isinstance(value, UUID) for value in self.known_evidence)
            or len(set(self.known_evidence)) != len(self.known_evidence)
        ):
            raise InvalidInputError("Known evidence must be distinct scoped UUID references.")
        if not isinstance(self.budget, tuple) or any(
            not isinstance(pair, tuple) or len(pair) != 2 for pair in self.budget
        ):
            raise InvalidInputError("Need budget must be immutable named limits.")
        if self.budget:
            if len(dict(self.budget)) != len(self.budget):
                raise InvalidInputError("Duplicate budget dimension.")
            validate_amounts(dict(self.budget))
        if len(set(self.query_profiles)) != len(self.query_profiles):
            raise InvalidInputError("Need retrieval profiles must be distinct.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "question": self.question,
            "pending_decision": self.pending_decision,
            "alternatives": list(self.alternatives),
            "required_evidence": list(self.required_evidence),
            "resolution_criteria": list(self.resolution_criteria),
            "scope": self.scope,
            "known_evidence": list(map(str, self.known_evidence)),
            "priority": self.priority,
            "budget": dict(self.budget),
            "query_profiles": list(self.query_profiles),
        }


class InformationNeedStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _revision(
        conn: Any,
        context: WorkspaceContext,
        need_id: UUID,
        spec: NeedSpec,
        predecessor: UUID | None,
    ) -> UUID:
        from psycopg.types.json import Jsonb

        for evidence in spec.known_evidence:
            if (
                conn.execute(
                    "SELECT id FROM evidence_events WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, evidence),
                ).fetchone()
                is None
            ):
                raise ScopeDeniedError("Evidence is unavailable in this workspace.")
        revision_id = uuid4()
        content = spec.to_dict()
        if spec.query_profiles:
            from potbot.semantics.releases import catalog_inventory, read_task_pin

            need = conn.execute(
                "SELECT episode_id FROM information_needs WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, need_id),
            ).fetchone()
            if need is None:
                raise ScopeDeniedError("Information need is unavailable in this workspace.")
            # The installation may have changed since this task started. Resolve
            # logical names only against the task's immutable semantic release.
            pin = read_task_pin(conn, context, need["episode_id"], required=True)
            assert pin is not None
            profiles = {
                row["name"]: row
                for row in catalog_inventory(conn, context, pin["release_id"], "retrieval_profile")
            }
            if not set(spec.query_profiles).issubset(profiles):
                raise InvalidInputError("Unknown retrieval profile in the task's pinned release.")
            content["semantic_release_id"] = str(pin["release_id"])
            content["resolved_query_profiles"] = [
                {
                    "name": name,
                    "id": profiles[name]["id"],
                    "content_hash": content_hash(profiles[name]),
                }
                for name in spec.query_profiles
            ]
        conn.execute(
            "INSERT INTO need_revisions(workspace_id,id,need_id,predecessor_id,content,content_digest) "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (
                context.workspace_id,
                revision_id,
                need_id,
                predecessor,
                Jsonb(content),
                content_hash(content),
            ),
        )
        for evidence in spec.known_evidence:
            conn.execute(
                "INSERT INTO need_revision_evidence(workspace_id,revision_id,evidence_id) VALUES(%s,%s,%s)",
                (context.workspace_id, revision_id, evidence),
            )
        return revision_id

    @staticmethod
    def _inspect(conn: Any, context: WorkspaceContext, need_id: UUID) -> dict[str, Any]:
        row = conn.execute(
            "SELECT n.*,s.current_revision_id,s.status,s.update_token,r.content,r.content_digest "
            "FROM information_needs n JOIN need_status s ON s.workspace_id=n.workspace_id AND s.need_id=n.id "
            "JOIN need_revisions r ON r.workspace_id=n.workspace_id AND r.id=s.current_revision_id "
            "WHERE n.workspace_id=%s AND n.id=%s",
            (context.workspace_id, need_id),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("Information need is unavailable in this workspace.")
        if content_hash(row["content"]) != row["content_digest"]:
            raise InvalidInputError("Information-need content integrity failed.")
        return dict(json_record(dict(row)))

    def create(
        self,
        context: WorkspaceContext,
        episode_id: UUID,
        spec: NeedSpec,
        idempotency_key: str,
        *,
        node_id: UUID | None = None,
    ) -> dict[str, Any]:
        request = {
            "episode_id": str(episode_id),
            "node_id": str(node_id) if node_id else None,
            "content": spec.to_dict(),
        }
        with self.database.transaction(context) as conn:

            def write() -> dict[str, Any]:
                lock_episode(conn, context, episode_id)
                need_id = uuid4()
                conn.execute(
                    "INSERT INTO information_needs(workspace_id,id,episode_id,node_id) VALUES(%s,%s,%s,%s)",
                    (context.workspace_id, need_id, episode_id, node_id),
                )
                revision = self._revision(conn, context, need_id, spec, None)
                conn.execute(
                    "INSERT INTO need_status(workspace_id,need_id,current_revision_id,status) VALUES(%s,%s,%s,'open')",
                    (context.workspace_id, need_id, revision),
                )
                append_event(
                    conn,
                    context,
                    "information_need_created",
                    episode_id=episode_id,
                    node_id=node_id,
                    details={"need_id": str(need_id), "revision_id": str(revision)},
                )
                return self._inspect(conn, context, need_id)

            return idempotent_command(
                conn, context, "create_information_need", idempotency_key, request, write
            )

    def revise(
        self,
        context: WorkspaceContext,
        need_id: UUID,
        expected_token: int,
        predecessor_id: UUID,
        spec: NeedSpec,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if type(expected_token) is not int or expected_token < 0:
            raise InvalidInputError("Invalid information-need update token.")
        request = {
            "need_id": str(need_id),
            "expected_token": expected_token,
            "predecessor_id": str(predecessor_id),
            "content": spec.to_dict(),
        }
        with self.database.transaction(context) as conn:

            def write() -> dict[str, Any]:
                row = conn.execute(
                    "SELECT * FROM need_status WHERE workspace_id=%s AND need_id=%s FOR UPDATE",
                    (context.workspace_id, need_id),
                ).fetchone()
                if row is None:
                    raise ScopeDeniedError("Information need is unavailable in this workspace.")
                if (
                    row["update_token"] != expected_token
                    or row["current_revision_id"] != predecessor_id
                ):
                    raise RevisionConflictError("Information-need revision is stale or mismatched.")
                revision = self._revision(conn, context, need_id, spec, predecessor_id)
                conn.execute(
                    "UPDATE need_status SET current_revision_id=%s,status='open',update_token=update_token+1,"
                    "updated_at=clock_timestamp() WHERE workspace_id=%s AND need_id=%s",
                    (revision, context.workspace_id, need_id),
                )
                return self._inspect(conn, context, need_id)

            return idempotent_command(
                conn, context, "revise_information_need", idempotency_key, request, write
            )

    def inspect(self, context: WorkspaceContext, need_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return self._inspect(conn, context, need_id)

    def history(self, context: WorkspaceContext, need_id: UUID) -> list[dict[str, Any]]:
        with self.database.transaction(context) as conn:
            self._inspect(conn, context, need_id)
            rows = conn.execute(
                "SELECT * FROM need_revisions WHERE workspace_id=%s AND need_id=%s ORDER BY created_at,id",
                (context.workspace_id, need_id),
            ).fetchall()
            for row in rows:
                if content_hash(row["content"]) != row["content_digest"]:
                    raise InvalidInputError("Information-need content integrity failed.")
                canonical_json(row["content"])
            return [dict(json_record(dict(row))) for row in rows]
