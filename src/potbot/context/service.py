"""Real Potbot PostgreSQL context service; never expose DB access to sandbox guests.

Every operation uses an authenticated WorkspaceContext checked against session_user
inside Database.transaction. Agent/worker transport must verify its operator
lease and obtain a trusted WorkspaceContext before calling this host-side API.
"""
from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from potbot.context.model import (
    document_templates,
    sha256,
    validate_content,
    validate_identity,
)
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.persistence.database import idempotent_command

if TYPE_CHECKING:
    from potbot.persistence.database import Database

_DENIED = "The requested resource is not accessible."


def _reason(value: str) -> str:
    if not isinstance(value, str) or len(value) > 1000 or "\x00" in value:
        raise InvalidInputError("Context change reason must contain at most 1000 characters.")
    return value


def _command_key(context: WorkspaceContext, key: str) -> str:
    # Existing command_results keys are workspace-scoped, not principal-scoped.
    # Never let a different workspace member replay someone else's result.
    if not isinstance(key, str) or not 1 <= len(key) <= 128 or "\x00" in key:
        raise InvalidInputError("An idempotency key of 1 to 128 characters is required.")
    return hashlib.sha256(context.principal_id.bytes + b"\x00" + key.encode()).hexdigest()


def _identity(context: WorkspaceContext) -> None:
    if not isinstance(context, WorkspaceContext):
        raise ScopeDeniedError(_DENIED)


class WorkspaceContextService:
    """Document CAS, history, object provisioning and a replayable event stream.

    Workspace visibility is intentionally shared. Private context is principal-
    scoped using PostgreSQL restrictive RLS. This service does not grant guest
    credentials, perform filesystem projection, or accept a caller-supplied actor.
    """

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _get_object(connection: Any, context: WorkspaceContext, kind: str, name: str) -> UUID:
        row = connection.execute(
            "SELECT id FROM workspace_context_objects "
            "WHERE workspace_id=%s AND kind=%s AND name=%s",
            (context.workspace_id, kind, name),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError(_DENIED)
        return row["id"]

    def read(self, context: WorkspaceContext, kind: str, name: str, role: str) -> dict[str, Any]:
        _identity(context)
        validate_identity(kind, name, role)
        with self.database.transaction(context) as conn:
            object_id = self._get_object(conn, context, kind, name)
            row = conn.execute(
                "SELECT content,revision,sha256 FROM workspace_context_documents "
                "WHERE workspace_id=%s AND object_id=%s AND role=%s",
                (context.workspace_id, object_id, role),
            ).fetchone()
            if row is None:
                raise ScopeDeniedError(_DENIED)
        return {"object_id": str(object_id), "role": role, **dict(row)}

    def read_triple(self, context: WorkspaceContext, kind: str,
                    name: str) -> dict[str, dict[str, Any]]:
        """Read exactly three documents in one PostgreSQL statement/snapshot."""
        from potbot.context.model import ROLES

        _identity(context)
        validate_identity(kind, name)
        with self.database.transaction(context) as conn:
            object_id = self._get_object(conn, context, kind, name)
            rows = conn.execute(
                "SELECT role,content,revision,sha256 FROM workspace_context_documents "
                "WHERE workspace_id=%s AND object_id=%s",
                (context.workspace_id, object_id),
            ).fetchall()
        docs = {row["role"]: dict(row) for row in rows}
        if set(docs) != set(ROLES):
            raise ScopeDeniedError("Context object does not have a complete document triple")
        for role, row in docs.items():
            validate_identity(kind, name, role)
            if sha256(row["content"]) != row["sha256"]:
                raise RevisionConflictError("Stored context digest is inconsistent")
        return docs

    def update(self, context: WorkspaceContext, kind: str, name: str, role: str,
               *, content: str, expected_revision: int, idempotency_key: str,
               reason: str = "") -> dict[str, Any]:
        """A CAS update and its history/outbox event commit atomically.

        Repeating the same idempotency key and identical arguments returns the
        original result, even after subsequent document revisions.
        """
        from psycopg.types.json import Jsonb

        _identity(context)
        validate_identity(kind, name, role)
        validate_content(content)
        reason = _reason(reason)
        if type(expected_revision) is not int or expected_revision < 1:
            raise InvalidInputError("Expected revision must be a positive integer.")
        checksum = sha256(content)
        with self.database.transaction(context) as conn:
            def perform() -> dict[str, Any]:
                object_id = self._get_object(conn, context, kind, name)
                row = conn.execute(
                    "SELECT revision,sha256 FROM workspace_context_documents "
                    "WHERE workspace_id=%s AND object_id=%s AND role=%s FOR UPDATE",
                    (context.workspace_id, object_id, role),
                ).fetchone()
                if row is None:
                    raise ScopeDeniedError(_DENIED)
                if row["revision"] != expected_revision:
                    raise RevisionConflictError("Context document revision is stale.")
                next_revision = expected_revision + 1
                conn.execute(
                    "UPDATE workspace_context_documents SET content=%s, revision=%s, "
                    "sha256=%s, updated_at=clock_timestamp() "
                    "WHERE workspace_id=%s AND object_id=%s AND role=%s",
                    (content, next_revision, checksum, context.workspace_id, object_id, role),
                )
                actor = f"principal:{context.principal_id}"
                conn.execute(
                    "INSERT INTO workspace_context_revisions "
                    "(workspace_id,object_id,role,revision,content,sha256,actor,reason) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (context.workspace_id, object_id, role, next_revision,
                     content, checksum, actor, reason),
                )
                conn.execute(
                    "INSERT INTO workspace_context_events "
                    "(workspace_id,object_id,role,revision,actor,event_type,payload) "
                    "VALUES (%s,%s,%s,%s,%s,'document.updated',%s)",
                    (context.workspace_id, object_id, role, next_revision, actor,
                     Jsonb({"previous_sha256": row["sha256"], "sha256": checksum})),
                )
                return {"object_id": str(object_id), "role": role,
                        "revision": next_revision, "sha256": checksum}

            return idempotent_command(
                conn, context, "context.update", _command_key(context, idempotency_key),
                {"kind": kind, "name": name, "role": role, "content_sha256": checksum,
                 "expected_revision": expected_revision, "reason": reason}, perform,
            )

    def provision_private(self, context: WorkspaceContext, kind: str, name: str,
                          *, idempotency_key: str) -> dict[str, Any]:
        """Creates a private triple owned by the authenticated principal.

        Shared objects, notably root sandbox context, can only be seeded by
        operator/admin bootstrap (not by this method).
        """
        from psycopg.types.json import Jsonb

        _identity(context)
        validate_identity(kind, name)
        if kind == "sandbox":
            raise ScopeDeniedError("Shared sandbox context requires operator provisioning.")
        with self.database.transaction(context) as conn:
            def perform() -> dict[str, Any]:
                object_id = uuid4()
                row = conn.execute(
                    "INSERT INTO workspace_context_objects "
                    "(workspace_id,id,kind,name,visibility,owner_principal_id) "
                    "VALUES (%s,%s,%s,%s,'private',%s) "
                    "ON CONFLICT (workspace_id,kind,name) DO NOTHING RETURNING id",
                    (context.workspace_id, object_id, kind, name, context.principal_id),
                ).fetchone()
                if row is None:
                    # ON CONFLICT may have matched a shared object or an
                    # invisible peer-owned object. Do not adopt either.
                    owned = conn.execute(
                        "SELECT id FROM workspace_context_objects "
                        "WHERE workspace_id=%s AND kind=%s AND name=%s "
                        "AND visibility='private' AND owner_principal_id=%s",
                        (context.workspace_id, kind, name, context.principal_id),
                    ).fetchone()
                    if owned is None:
                        raise ScopeDeniedError(_DENIED)
                    object_id = owned["id"]
                else:
                    object_id = row["id"]
                actor = f"principal:{context.principal_id}"
                for role, content in document_templates(kind, name).items():
                    checksum = sha256(content)
                    created = conn.execute(
                        "INSERT INTO workspace_context_documents "
                        "(workspace_id,object_id,role,content,revision,sha256) "
                        "VALUES (%s,%s,%s,%s,1,%s) ON CONFLICT DO NOTHING RETURNING revision",
                        (context.workspace_id, object_id, role, content, checksum),
                    ).fetchone()
                    if created is None:
                        continue
                    conn.execute(
                        "INSERT INTO workspace_context_revisions "
                        "(workspace_id,object_id,role,revision,content,sha256,actor,reason) "
                        "VALUES (%s,%s,%s,1,%s,%s,%s,'initial context')",
                        (context.workspace_id, object_id, role, content, checksum, actor),
                    )
                    conn.execute(
                        "INSERT INTO workspace_context_events "
                        "(workspace_id,object_id,role,revision,actor,event_type,payload) "
                        "VALUES (%s,%s,%s,1,%s,'document.provisioned',%s)",
                        (context.workspace_id, object_id, role, actor,
                         Jsonb({"sha256": checksum})),
                    )
                return {"object_id": str(object_id), "kind": kind, "name": name}

            return idempotent_command(
                conn, context, "context.provision_private", _command_key(context, idempotency_key),
                {"kind": kind, "name": name}, perform,
            )

    def history(self, context: WorkspaceContext, kind: str, name: str, role: str,
                *, limit: int = 50) -> list[dict[str, Any]]:
        _identity(context)
        validate_identity(kind, name, role)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise InvalidInputError("Context history limit must be between 1 and 200.")
        with self.database.transaction(context) as conn:
            object_id = self._get_object(conn, context, kind, name)
            rows = conn.execute(
                "SELECT revision,sha256,actor,reason,created_at "
                "FROM workspace_context_revisions "
                "WHERE workspace_id=%s AND object_id=%s AND role=%s "
                "ORDER BY revision DESC LIMIT %s",
                (context.workspace_id, object_id, role, limit),
            ).fetchall()
        return [{**dict(row), "created_at": row["created_at"].isoformat()} for row in rows]

    def events(self, context: WorkspaceContext, *, after_id: int = 0,
               limit: int = 100) -> list[dict[str, Any]]:
        """Durable, at-least-once event stream. Consumers persist their cursor.

        RLS joins prevent leaking notifications about private context objects.
        No exactly-once external delivery is promised.
        """
        _identity(context)
        if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidInputError("Context event cursor or limit is invalid.")
        with self.database.transaction(context) as conn:
            rows = conn.execute(
                "SELECT e.id,e.object_id,e.role,e.revision,e.actor,e.event_type,e.payload,e.created_at "
                "FROM workspace_context_events e "
                "JOIN workspace_context_objects o "
                "ON (o.workspace_id=e.workspace_id AND o.id=e.object_id) "
                "WHERE e.workspace_id=%s AND e.id>%s ORDER BY e.id LIMIT %s",
                (context.workspace_id, after_id, limit),
            ).fetchall()
        return [{**dict(row), "object_id": str(row["object_id"]),
                 "created_at": row["created_at"].isoformat()} for row in rows]
