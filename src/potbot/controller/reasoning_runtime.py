"""PostgreSQL-fenced reasoning snapshots and workspace-scoped steering.

This boundary serializes *one writer per reasoning run*, even across API
processes. Expired runs are marked indeterminate; no uncertain model/tool call
is automatically repeated. Stored snapshots are retained as audit evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from uuid import UUID, uuid4

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.persistence.database import Database

_RUN = re.compile(r"[0-9a-f]{32}\Z")
_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


def _run_id(value: str) -> UUID:
    if not isinstance(value, str) or not _RUN.fullmatch(value):
        raise InvalidInputError("reasoning run ID must be a 32-character hex identifier")
    return UUID(hex=value)


def _record(row: Any) -> dict[str, Any]:
    if row is None:
        raise ScopeDeniedError("reasoning run is unavailable")
    return dict(row)


class PostgresReasoningStore:
    """Run-scoped lease with transactional snapshots and committed event replay."""

    def __init__(self, database: Database, *, lease_seconds: int = 600) -> None:
        if type(lease_seconds) is not int or not 60 <= lease_seconds <= 3600:
            raise InvalidInputError("reasoning lease must be 60-3600 seconds")
        self.database = database
        self.lease_seconds = lease_seconds

    def start(self, context: WorkspaceContext, run_id: str) -> UUID:
        token = uuid4()
        with self.database.transaction(context) as conn:
            conn.execute(
                "INSERT INTO reasoning_runtime_runs "
                "(workspace_id,id,principal_id,state,lease_token,lease_until) "
                "VALUES (%s,%s,%s,'running',%s,clock_timestamp()+(%s * interval '1 second'))",
                (context.workspace_id, _run_id(run_id), context.principal_id, token,
                 self.lease_seconds),
            )
        return token

    def checkpoint(self, context: WorkspaceContext, run_id: str, token: UUID,
                   snapshot: dict[str, Any]) -> None:
        """Fenced checkpoint and event append commit atomically.

        An expired writer cannot publish a late model result. A later operator
        must reconcile the uncertain invocation rather than blindly replay it.
        """
        if not isinstance(snapshot, dict) or len(json.dumps(snapshot).encode()) > 4_194_304:
            raise InvalidInputError("reasoning snapshot exceeds the allowed bound")
        events = snapshot.get("events")
        if not isinstance(events, list) or len(events) > 10000:
            raise InvalidInputError("reasoning event history is invalid")
        status = snapshot.get("run_status", "running")
        if status not in {"running", "completed", "failed"}:
            raise InvalidInputError("reasoning run status is invalid")
        from psycopg.types.json import Jsonb

        with self.database.transaction(context) as conn:
            row = _record(conn.execute(
                "SELECT state,lease_token,event_cursor FROM reasoning_runtime_runs "
                "WHERE workspace_id=%s AND id=%s FOR UPDATE",
                (context.workspace_id, _run_id(run_id)),
            ).fetchone())
            valid = conn.execute(
                "SELECT 1 FROM reasoning_runtime_runs WHERE workspace_id=%s AND id=%s "
                "AND state='running' AND lease_token=%s AND lease_until > clock_timestamp()",
                (context.workspace_id, _run_id(run_id), token),
            ).fetchone()
            if valid is None:
                raise StateMismatchError("reasoning lease expired or was superseded")
            cursor = row["event_cursor"]
            if cursor > len(events):
                raise StateMismatchError("reasoning events cannot be truncated")
            for index, event in enumerate(events[cursor:], start=cursor + 1):
                if not isinstance(event, dict) or event.get("sequence") != index:
                    raise InvalidInputError("reasoning events must be append-only and ordered")
                conn.execute(
                    "INSERT INTO reasoning_runtime_events "
                    "(workspace_id,run_id,sequence,type,data) VALUES (%s,%s,%s,%s,%s)",
                    (context.workspace_id, _run_id(run_id), index, event.get("type", ""),
                     Jsonb(event)),
                )
            closed = status != "running"
            conn.execute(
                "UPDATE reasoning_runtime_runs SET snapshot=%s,checkpoint=checkpoint+1, "
                "event_cursor=%s,state=%s,lease_token=%s,lease_until="
                "CASE WHEN %s THEN NULL ELSE clock_timestamp()+(%s * interval '1 second') END, "
                "accepting_steering=CASE WHEN %s THEN false ELSE accepting_steering END, "
                "updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s",
                (Jsonb(snapshot), len(events), status, None if closed else token,
                 closed, self.lease_seconds, closed, context.workspace_id, _run_id(run_id)),
            )

    def inspect(self, context: WorkspaceContext, run_id: str) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            row = _record(conn.execute(
                "SELECT state,generation,checkpoint,accepting_steering,snapshot "
                "FROM reasoning_runtime_runs WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, _run_id(run_id)),
            ).fetchone())
        return {"run_id": run_id, **row}

    def events(self, context: WorkspaceContext, run_id: str, *, after: int = 0,
               limit: int = 100) -> list[dict[str, Any]]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise InvalidInputError("invalid reasoning event cursor")
        with self.database.transaction(context) as conn:
            _record(conn.execute(
                "SELECT id FROM reasoning_runtime_runs WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, _run_id(run_id)),
            ).fetchone())
            rows = conn.execute(
                "SELECT sequence,type,data FROM reasoning_runtime_events "
                "WHERE workspace_id=%s AND run_id=%s AND sequence>%s "
                "ORDER BY sequence LIMIT %s",
                (context.workspace_id, _run_id(run_id), after, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def expire(self, context: WorkspaceContext, *, limit: int = 100) -> int:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise InvalidInputError("invalid recovery limit")
        with self.database.transaction(context) as conn:
            rows = conn.execute(
                "SELECT id FROM reasoning_runtime_runs WHERE workspace_id=%s "
                "AND state='running' AND lease_until <= clock_timestamp() "
                "ORDER BY lease_until,id FOR UPDATE SKIP LOCKED LIMIT %s",
                (context.workspace_id, limit),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE reasoning_runtime_runs SET state='indeterminate',lease_token=NULL, "
                    "lease_until=NULL,accepting_steering=false,updated_at=clock_timestamp() "
                    "WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, row["id"]),
                )
        return len(rows)


class PostgresSteeringInbox:
    """Durable concurrent intake; only the run owner publishes steering decisions."""

    uses_context = True

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _key(request_id: str) -> str:
        if not isinstance(request_id, str) or not _KEY.fullmatch(request_id):
            raise InvalidInputError("steering request ID is invalid")
        return hashlib.sha256(request_id.encode()).hexdigest()

    @staticmethod
    def _view(row: dict[str, Any]) -> dict[str, Any]:
        result = {"id": row["id"], "request_id": row["request_id"],
                  "prompt": row["prompt"], "status": row["status"],
                  "created_ns": int(row["created_at"].timestamp() * 1_000_000_000)}
        if row["decision"] is not None:
            result.update(row["decision"])
        return result

    def submit(self, context: WorkspaceContext, run_id: str, *, request_id: str,
               prompt: str) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 8192:
            raise InvalidInputError("steering prompt must be 1-8192 UTF-8 bytes")
        key = self._key(request_id)
        with self.database.transaction(context) as conn:
            run = _record(conn.execute(
                "SELECT state,accepting_steering FROM reasoning_runtime_runs "
                "WHERE workspace_id=%s AND id=%s FOR UPDATE",
                (context.workspace_id, _run_id(run_id)),
            ).fetchone())
            existing = conn.execute(
                "SELECT * FROM reasoning_runtime_steering "
                "WHERE workspace_id=%s AND run_id=%s AND id=%s",
                (context.workspace_id, _run_id(run_id), key),
            ).fetchone()
            if existing:
                if existing["prompt"] != prompt or existing["request_id"] != request_id:
                    raise InvalidInputError("steering idempotency conflict")
                return self._view(existing)
            if run["state"] != "running" or not run["accepting_steering"]:
                raise InvalidInputError("reasoning run is no longer accepting steering")
            row = conn.execute(
                "INSERT INTO reasoning_runtime_steering "
                "(workspace_id,run_id,id,request_id,prompt,status) "
                "VALUES (%s,%s,%s,%s,%s,'queued') RETURNING *",
                (context.workspace_id, _run_id(run_id), key, request_id, prompt),
            ).fetchone()
            return self._view(_record(row))

    def pending(self, context: WorkspaceContext, run_id: str, *, limit: int = 16) -> tuple[dict[str, Any], ...]:
        if type(limit) is not int or not 1 <= limit <= 64:
            raise InvalidInputError("invalid steering page size")
        with self.database.transaction(context) as conn:
            rows = conn.execute(
                "SELECT * FROM reasoning_runtime_steering WHERE workspace_id=%s "
                "AND run_id=%s AND status='queued' ORDER BY created_at,id LIMIT %s",
                (context.workspace_id, _run_id(run_id), limit),
            ).fetchall()
        return tuple(self._view(dict(row)) for row in rows)

    def resolve(self, context: WorkspaceContext, run_id: str, request: dict[str, Any],
                decision: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(decision, dict) or len(json.dumps(decision)) > 8192:
            raise InvalidInputError("steering decision must be bounded")
        from psycopg.types.json import Jsonb

        with self.database.transaction(context) as conn:
            row = _record(conn.execute(
                "SELECT * FROM reasoning_runtime_steering WHERE workspace_id=%s "
                "AND run_id=%s AND id=%s FOR UPDATE",
                (context.workspace_id, _run_id(run_id), request["id"]),
            ).fetchone())
            if row["status"] == "decided":
                return self._view(row)
            result = conn.execute(
                "UPDATE reasoning_runtime_steering SET status='decided',decision=%s "
                "WHERE workspace_id=%s AND run_id=%s AND id=%s RETURNING *",
                (Jsonb(decision), context.workspace_id, _run_id(run_id), request["id"]),
            ).fetchone()
            return self._view(_record(result))

    def seal(self, context: WorkspaceContext, run_id: str) -> None:
        with self.database.transaction(context) as conn:
            conn.execute(
                "UPDATE reasoning_runtime_runs SET accepting_steering=false "
                "WHERE workspace_id=%s AND id=%s AND state='running'",
                (context.workspace_id, _run_id(run_id)),
            )

    def get(self, context: WorkspaceContext, run_id: str, request_id: str) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            row = conn.execute(
                "SELECT * FROM reasoning_runtime_steering WHERE workspace_id=%s "
                "AND run_id=%s AND id=%s",
                (context.workspace_id, _run_id(run_id), self._key(request_id)),
            ).fetchone()
            return self._view(_record(row))

    def history(self, context: WorkspaceContext, run_id: str) -> list[dict[str, Any]]:
        with self.database.transaction(context) as conn:
            _record(conn.execute(
                "SELECT id FROM reasoning_runtime_runs WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, _run_id(run_id)),
            ).fetchone())
            rows = conn.execute(
                "SELECT * FROM reasoning_runtime_steering WHERE workspace_id=%s "
                "AND run_id=%s ORDER BY created_at,id LIMIT 257",
                (context.workspace_id, _run_id(run_id)),
            ).fetchall()
        if len(rows) > 256:
            raise InvalidInputError("steering history exceeds page limit")
        return [self._view(dict(row)) for row in rows]
