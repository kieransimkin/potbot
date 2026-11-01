"""PostgreSQL-authoritative context leases; only the trusted host holds grants.

A guest cannot choose principal, agent, object name, session, or generation.
Session authentication, CAS, journal and RPC receipt share ONE transaction.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from potbot.context.model import sandbox_object_id, sha256
from potbot.context.rpc_contract import ContextRequest
from potbot.context.service import WorkspaceContextService
from potbot.context.transactions import ContextDatabase
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError

_DENIED = "Context session is unavailable or no longer authorized"
_SECRET = re.compile(r"[A-Za-z0-9_-]{43}\Z")


@dataclass(frozen=True, slots=True)
class ContextSession:
    """Host-only capability. Never serialize this value to a guest or audit log."""

    principal: WorkspaceContext
    session_id: UUID
    agent_id: UUID
    run_id: UUID
    generation: int
    secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if (not isinstance(self.principal, WorkspaceContext)
                or any(not isinstance(x, UUID) for x in
                       (self.session_id, self.agent_id, self.run_id))
                or type(self.generation) is not int or self.generation < 1
                or not isinstance(self.secret, str) or not _SECRET.fullmatch(self.secret)):
            raise InvalidInputError("Invalid host context capability")


class _BoundTransaction:
    """Borrow a verified transaction. No second connection, commit, or identity switch."""

    def __init__(self, principal: WorkspaceContext, connection: Any) -> None:
        self.principal, self.connection = principal, connection

    @contextmanager
    def transaction(self, context: WorkspaceContext) -> Iterator[Any]:
        if context != self.principal:
            raise ScopeDeniedError(_DENIED)
        yield self.connection


def _seconds(value: int) -> int:
    if type(value) is not int or not 5 <= value <= 300:
        raise InvalidInputError("Context lease duration must be 5-300 seconds")
    return value


class ContextSessionStore:
    def __init__(self, database: ContextDatabase) -> None:
        self.database = database

    @staticmethod
    def _now(conn: Any) -> datetime:
        # Deliberately executed AFTER row-lock acquisition, not transaction-start now().
        row = conn.execute("SELECT pg_catalog.clock_timestamp() AS now").fetchone()
        value = row["now"]
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ScopeDeniedError("Context database clock is unavailable")
        return value

    def issue(self, principal: WorkspaceContext, agent_id: UUID, run_id: UUID, *,
              lease_seconds: int = 60, request_limit: int = 256) -> ContextSession:
        """Host-only; caller must independently admit this registered worker run."""
        duration = _seconds(lease_seconds)
        if (not isinstance(principal, WorkspaceContext) or not isinstance(agent_id, UUID)
                or not isinstance(run_id, UUID) or type(request_limit) is not int
                or not 1 <= request_limit <= 1024):
            raise InvalidInputError("Context session needs a trusted worker binding")
        secret = secrets.token_urlsafe(32)
        session_id = uuid4()
        with self.database.transaction(principal) as conn:
            obj = conn.execute(
                "SELECT id FROM workspace_context_objects WHERE workspace_id=%s "
                "AND kind='agent' AND name=%s AND visibility='private' AND owner_principal_id=%s",
                (principal.workspace_id, str(agent_id), principal.principal_id),
            ).fetchone()
            if obj is None:
                raise ScopeDeniedError(_DENIED)
            now = self._now(conn)
            conn.execute(
                "INSERT INTO workspace_context_sessions "
                "(workspace_id,id,principal_id,agent_object_id,agent_id,run_id,secret_sha256,"
                "created_at,expires_at,deadline_at,request_limit) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s + %s * interval '1 second',"
                "%s + interval '1 hour',%s)",
                (principal.workspace_id, session_id, principal.principal_id, obj["id"], agent_id,
                 run_id, hashlib.sha256(secret.encode("ascii")).hexdigest(), now, now,
                 duration, now, request_limit),
            )
        return ContextSession(principal, session_id, agent_id, run_id, 1, secret)

    def _lock(self, conn: Any, grant: ContextSession, *, live: bool = True) -> dict[str, Any]:
        if not isinstance(grant, ContextSession):
            raise ScopeDeniedError(_DENIED)
        row = conn.execute(
            "SELECT * FROM workspace_context_sessions WHERE workspace_id=%s AND id=%s "
            "AND principal_id=%s FOR UPDATE",
            (grant.principal.workspace_id, grant.session_id, grant.principal.principal_id),
        ).fetchone()
        if (row is None or row["agent_id"] != grant.agent_id or row["run_id"] != grant.run_id
                or not hmac.compare_digest(row["secret_sha256"],
                    hashlib.sha256(grant.secret.encode("ascii")).hexdigest())):
            raise ScopeDeniedError(_DENIED)
        if live and (row["state"] != "active" or row["generation"] != grant.generation
                     or row["expires_at"] <= self._now(conn)):
            raise ScopeDeniedError(_DENIED)
        return dict(row)

    def renew(self, grant: ContextSession, *, lease_seconds: int = 60) -> None:
        if not isinstance(grant, ContextSession):
            raise ScopeDeniedError(_DENIED)
        duration = _seconds(lease_seconds)
        with self.database.transaction(grant.principal) as conn:
            self._lock(conn, grant)
            conn.execute(
                "UPDATE workspace_context_sessions SET expires_at=LEAST(deadline_at,"
                "pg_catalog.clock_timestamp()+%s*interval '1 second') WHERE workspace_id=%s AND id=%s",
                (duration, grant.principal.workspace_id, grant.session_id),
            )

    def revoke(self, grant: ContextSession) -> None:
        """Serializes with in-flight operations; an expired grant may still be revoked."""
        if not isinstance(grant, ContextSession):
            raise ScopeDeniedError(_DENIED)
        with self.database.transaction(grant.principal) as conn:
            row = self._lock(conn, grant, live=False)
            if row["state"] == "revoked":
                return
            if row["generation"] != grant.generation:
                raise ScopeDeniedError(_DENIED)
            conn.execute(
                "UPDATE workspace_context_sessions SET state='revoked',generation=generation+1,"
                "revoked_at=pg_catalog.clock_timestamp() WHERE workspace_id=%s AND id=%s",
                (grant.principal.workspace_id, grant.session_id),
            )

    def execute(self, grant: ContextSession, request: ContextRequest) -> dict[str, Any]:
        """A valid live lease is required even for an idempotent response replay."""
        if not isinstance(grant, ContextSession) or not isinstance(request, ContextRequest):
            raise ScopeDeniedError(_DENIED)
        context = grant.principal
        with self.database.transaction(context) as conn:
            session = self._lock(conn, grant)
            service = WorkspaceContextService(_BoundTransaction(context, conn))
            kind = request.scope
            name = str(grant.agent_id if kind == "agent" else context.workspace_id)
            object_id = (session["agent_object_id"] if kind == "agent"
                         else sandbox_object_id(context.workspace_id))
            # Recheck rights before replay as well as before creating any new revision.
            # FOR SHARE keeps a granted row from being revoked midway through this transaction.
            if request.method == "context.update" and kind == "sandbox":
                allowed = conn.execute(
                    "SELECT role FROM workspace_context_write_grants WHERE workspace_id=%s "
                    "AND object_id=%s AND principal_id=%s AND role=%s FOR SHARE",
                    (context.workspace_id, object_id, context.principal_id, request.role),
                ).fetchone()
                if allowed is None:
                    raise ScopeDeniedError(_DENIED)
            if request.method == "context.update":
                replay = conn.execute(
                    "SELECT request_sha256,result FROM workspace_context_session_receipts "
                    "WHERE workspace_id=%s AND session_id=%s AND request_id=%s",
                    (context.workspace_id, grant.session_id, request.request_id),
                ).fetchone()
                if replay is not None:
                    if replay["request_sha256"] != request.fingerprint:
                        raise RevisionConflictError("Request ID was used for different content")
                    self._check_expiry(conn, session)
                    return dict(replay["result"])
            if session["request_count"] >= session["request_limit"]:
                raise ScopeDeniedError("Context session request budget exhausted")
            if request.method == "context.read":
                result = service.read(context, kind, name, request.role)
                if sha256(result["content"]) != result["sha256"]:
                    raise RevisionConflictError("Context document digest mismatch")
            else:
                from psycopg.types.json import Jsonb

                assert request.content is not None and request.expected_revision is not None
                key = hashlib.sha256(
                    grant.session_id.bytes + request.request_id.bytes
                ).hexdigest()
                result = service.update(
                    context, kind, name, request.role, content=request.content,
                    expected_revision=request.expected_revision, idempotency_key="agent-rpc:"+key,
                    reason=request.reason,
                )
                conn.execute(
                    "INSERT INTO workspace_context_session_receipts "
                    "(workspace_id,session_id,request_id,request_sha256,object_id,role,revision,result) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (context.workspace_id, grant.session_id, request.request_id, request.fingerprint,
                     object_id, request.role, result["revision"], Jsonb(result)),
                )
            # A document lock may have blocked until after the lease expired.
            # Reject here to roll back the CAS, history, event and receipt together.
            self._check_expiry(conn, session)
            conn.execute(
                "UPDATE workspace_context_sessions SET request_count=request_count+1 "
                "WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, grant.session_id),
            )
            return result

    def _check_expiry(self, conn: Any, session: dict[str, Any]) -> None:
        if session["expires_at"] <= self._now(conn):
            raise ScopeDeniedError(_DENIED)
