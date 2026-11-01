"""PostgreSQL-authoritative context leases; only the trusted host holds grants.

A guest cannot choose principal, agent, object name, session, or generation.
Session authentication, CAS, journal and RPC receipt share ONE transaction.
"""

from __future__ import annotations

import hashlib
import json
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
from potbot.context.rpc_contract import CatalogueRequest, ContextRequest, OperationRequest, SkillRequest, WorkRequest
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
            # A run has a separate working model/history, provisioned in this
            # same transaction. Agent identity was already verified above.
            WorkspaceContextService(_BoundTransaction(principal, conn)).provision_private(
                principal, "worker", worker_context_name(agent_id, run_id),
                idempotency_key="worker-context:" + agent_id.hex + run_id.hex,
            )
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

    def _lock(self, conn: Any, grant: ContextSession, *, live: bool = True, allow_result: bool = False) -> dict[str, Any]:
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
        if live and not allow_result and row.get("result_request_id") is not None:
            raise ScopeDeniedError("Context session has completed its work")
        return dict(row)

    def renew(self, grant: ContextSession, *, lease_seconds: int = 60) -> None:
        if not isinstance(grant, ContextSession):
            raise ScopeDeniedError(_DENIED)
        duration = _seconds(lease_seconds)
        with self.database.transaction(grant.principal) as conn:
            self._lock(conn, grant, allow_result=True)
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

    def admit_work_read(self, grant: ContextSession) -> None:
        """A host-bound input disclosure consumes the same live session budget."""
        with self.database.transaction(grant.principal) as conn:
            session = self._lock(conn, grant)
            if session["request_count"] >= session["request_limit"]:
                raise ScopeDeniedError("Context session request budget exhausted")
            self._check_expiry(conn, session)
            conn.execute("UPDATE workspace_context_sessions SET request_count=request_count+1 "
                         "WHERE workspace_id=%s AND id=%s",
                         (grant.principal.workspace_id, grant.session_id))

    def complete_work(self, grant: ContextSession, request: WorkRequest) -> dict[str, Any]:
        """Persist one final result, then fence subsequent context mutations.

        A receipt only acknowledges storage. The controller still requires a
        zero exit code and independent process-tree/audit shutdown evidence.
        """
        if not isinstance(grant, ContextSession) or not isinstance(request, WorkRequest) or request.method != "work.result":
            raise ScopeDeniedError(_DENIED)
        request = WorkRequest(request.request_id, request.method, request.result)
        raw = json.dumps(request.result, ensure_ascii=False, allow_nan=False,
                         sort_keys=True, separators=(",", ":"))
        checksum = hashlib.sha256(raw.encode()).hexdigest()
        context = grant.principal
        with self.database.transaction(context) as conn:
            session = self._lock(conn, grant, allow_result=True)
            previous = conn.execute(
                "SELECT request_id,request_sha256,result_sha256 FROM workspace_context_work_results "
                "WHERE workspace_id=%s AND session_id=%s",
                (context.workspace_id, grant.session_id),
            ).fetchone()
            if previous is not None:
                if previous["request_id"] != request.request_id or previous["request_sha256"] != request.fingerprint:
                    raise RevisionConflictError("Work already has a different result")
                self._check_expiry(conn, session)
                return {"revision": 1, "sha256": previous["result_sha256"]}
            reused = conn.execute(
                "SELECT 1 FROM workspace_context_session_receipts WHERE workspace_id=%s AND session_id=%s AND request_id=%s "
                "UNION ALL SELECT 1 FROM workspace_context_skill_receipts WHERE workspace_id=%s AND session_id=%s AND request_id=%s",
                (context.workspace_id, grant.session_id, request.request_id,
                 context.workspace_id, grant.session_id, request.request_id),
            ).fetchone()
            if reused is not None:
                raise RevisionConflictError("Request ID was used for another operation")
            if session["request_count"] >= session["request_limit"]:
                raise ScopeDeniedError("Context session request budget exhausted")
            conn.execute(
                "INSERT INTO workspace_context_work_results "
                "(workspace_id,session_id,request_id,request_sha256,result_json,result_sha256) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (context.workspace_id, grant.session_id, request.request_id,
                 request.fingerprint, raw, checksum),
            )
            conn.execute(
                "UPDATE workspace_context_sessions SET result_request_id=%s,request_count=request_count+1 "
                "WHERE workspace_id=%s AND id=%s",
                (request.request_id, context.workspace_id, grant.session_id),
            )
            self._check_expiry(conn, session)
            return {"revision": 1, "sha256": checksum}

    def ensure_agent_context(self, principal: WorkspaceContext, agent_id: UUID) -> None:
        """Host lifecycle hook; caller MUST first validate a registered home/run.

        ContextWorkerChannel calls this only after its independent process/home
        binding checks. This is not exposed as a guest RPC or authorization API.
        """
        if not isinstance(principal, WorkspaceContext) or not isinstance(agent_id, UUID):
            raise ScopeDeniedError(_DENIED)
        WorkspaceContextService(self.database).provision_private(
            principal, "agent", str(agent_id), idempotency_key="agent-context:" + agent_id.hex,
        )

    def execute(self, grant: ContextSession, request: OperationRequest) -> dict[str, Any]:
        """Session, ownership, CAS, skill lifecycle and receipts share one commit."""
        from potbot.context.skill_catalogue import SessionSkillCatalogue

        if not isinstance(grant, ContextSession) or not isinstance(request, (ContextRequest, SkillRequest, CatalogueRequest)):
            raise ScopeDeniedError(_DENIED)
        context = grant.principal
        with self.database.transaction(context) as conn:
            session = self._lock(conn, grant)
            service = WorkspaceContextService(_BoundTransaction(context, conn))
            skills = SessionSkillCatalogue(conn, grant, session, service)
            mutates = request.mutates if isinstance(request, SkillRequest) else request.method == "context.update"
            if isinstance(request, SkillRequest):
                skills.authorize(request)
            elif isinstance(request, CatalogueRequest):
                pass
            else:
                kind = request.scope
                if kind == "object":
                    from potbot.context.catalogue import object_target

                    assert request.object_id is not None
                    kind, name = object_target(conn, context, request.object_id, request.role, write=mutates)
                    object_id = request.object_id
                elif kind == "skill":
                    assert request.skill_id is not None
                    name, object_id = skills.context_target(request.skill_id, write=mutates)
                elif kind == "worker":
                    name = worker_context_name(grant.agent_id, grant.run_id)
                    object_id = service._get_object(conn, context, kind, name)
                else:
                    name = str(grant.agent_id if kind == "agent" else context.workspace_id)
                    object_id = (session["agent_object_id"] if kind == "agent"
                                 else sandbox_object_id(context.workspace_id))
                if mutates and kind == "sandbox":
                    # Includes AGENTS.md: a filename grants no authority. Only an
                    # explicit operator-issued role grant permits this mutation.
                    allowed = conn.execute(
                        "SELECT role FROM workspace_context_write_grants WHERE workspace_id=%s "
                        "AND object_id=%s AND principal_id=%s AND role=%s FOR SHARE",
                        (context.workspace_id, object_id, context.principal_id, request.role),
                    ).fetchone()
                    if allowed is None:
                        raise ScopeDeniedError(_DENIED)
            # The session row lock makes request UUIDs unique across both kinds
            # of receipt. Moving a UUID between context and skill methods fails.
            replays = conn.execute(
                "SELECT request_sha256,result FROM workspace_context_session_receipts "
                "WHERE workspace_id=%s AND session_id=%s AND request_id=%s UNION ALL "
                "SELECT request_sha256,result FROM workspace_context_skill_receipts "
                "WHERE workspace_id=%s AND session_id=%s AND request_id=%s",
                (context.workspace_id, grant.session_id, request.request_id,
                 context.workspace_id, grant.session_id, request.request_id),
            ).fetchall()
            if replays:
                if len(replays) != 1 or not mutates or replays[0]["request_sha256"] != request.fingerprint:
                    raise RevisionConflictError("Request ID was used for a different operation")
                self._check_expiry(conn, session)
                return dict(replays[0]["result"])
            if session["request_count"] >= session["request_limit"]:
                raise ScopeDeniedError("Context session request budget exhausted")
            if isinstance(request, CatalogueRequest):
                from potbot.context.catalogue import list_objects

                result = list_objects(conn, context, after=request.after, limit=request.limit)
            elif isinstance(request, SkillRequest):
                result = skills.execute(request)
                if mutates:
                    from psycopg.types.json import Jsonb

                    conn.execute(
                        "INSERT INTO workspace_context_skill_receipts "
                        "(workspace_id,session_id,request_id,request_sha256,skill_id,result) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        (context.workspace_id, grant.session_id, request.request_id,
                         request.fingerprint, UUID(result["skill_id"]), Jsonb(result)),
                    )
            elif request.method == "context.read":
                result = service.read(context, kind, name, request.role)
                if sha256(result["content"]) != result["sha256"]:
                    raise RevisionConflictError("Context document digest mismatch")
            else:
                from psycopg.types.json import Jsonb

                assert request.content is not None and request.expected_revision is not None
                key = hashlib.sha256(grant.session_id.bytes + request.request_id.bytes).hexdigest()
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
            self._check_expiry(conn, session)
            conn.execute(
                "UPDATE workspace_context_sessions SET request_count=request_count+1 "
                "WHERE workspace_id=%s AND id=%s", (context.workspace_id, grant.session_id),
            )
            return result

    def _check_expiry(self, conn: Any, session: dict[str, Any]) -> None:
        if session["expires_at"] <= self._now(conn):
            raise ScopeDeniedError(_DENIED)


def worker_context_name(agent_id: UUID, run_id: UUID) -> str:
    """Do not collapse two agents working on the same run into one object."""
    if not isinstance(agent_id, UUID) or not isinstance(run_id, UUID):
        raise InvalidInputError("Worker context needs immutable agent and run identities")
    return agent_id.hex + "." + run_id.hex
