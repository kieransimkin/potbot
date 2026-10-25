"""Durable chat admission for Potbot's existing workspace-scoped operator UI.

Before each inference the user turn is committed and the prompt is reconstructed
from stored history. The BFF must acknowledge a terminal SSE result to mark the
turn completed. Unknown outcomes remain indeterminate, never replayed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from fastapi import Request

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError

if TYPE_CHECKING:
    from fastapi import FastAPI
    from potbot.persistence.database import Database

MAX_PROMPT_BYTES = 48000


def _title(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 120 or any(ord(c) < 32 for c in value):
        raise InvalidInputError("A bounded visible session title is required")
    return value.strip()


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise InvalidInputError("Chat text is empty or exceeds the content limit")
    return value


def _uuid(value: Any) -> UUID:
    try:
        if not isinstance(value, str):
            raise ValueError
        parsed = UUID(value)
        if str(parsed) != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError, AttributeError):
        raise InvalidInputError("Expected canonical chat UUID") from None


class UiChatStore:
    """One principal's bounded chat history, fenced by a workspace transaction."""

    def __init__(self, database: Database) -> None:
        self.db = database

    @staticmethod
    def _summary(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": str(row["id"]), "title": row["title"],
                "revision": int(row["revision"]),
                "updatedAt": row["updated_at"].isoformat()}

    def create_session(self, context: WorkspaceContext, *, session_id: UUID, title: str) -> dict[str, Any]:
        title = _title(title)
        with self.db.transaction(context) as conn:
            # Serialize admission per principal so a second operator tab cannot
            # bypass the bounded-session allowance concurrently.
            conn.execute("SELECT pg_catalog.pg_advisory_xact_lock(hashtext(%s))",
                         ("potbot:ui:chat:" + str(context.workspace_id) + ":" +
                          str(context.principal_id),))
            known = conn.execute(
                "SELECT id,title,revision,updated_at FROM ui_chat_sessions "
                "WHERE workspace_id=%s AND id=%s AND principal_id=%s",
                (context.workspace_id, session_id, context.principal_id),
            ).fetchone()
            if known:
                if known["title"] != title:
                    raise RevisionConflictError("Chat session ID already allocated")
                return {**self._summary(known), "created": False}
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM ui_chat_sessions WHERE workspace_id=%s AND principal_id=%s",
                (context.workspace_id, context.principal_id),
            ).fetchone()
            if count["n"] >= 100:
                raise InvalidInputError("Chat history reached its session allowance")
            row = conn.execute(
                "INSERT INTO ui_chat_sessions(workspace_id,id,principal_id,title) VALUES(%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING RETURNING id,title,revision,updated_at",
                (context.workspace_id, session_id, context.principal_id, title),
            ).fetchone()
            if row:
                return {**self._summary(row), "created": True}
            existing = conn.execute(
                "SELECT id,title,revision,updated_at FROM ui_chat_sessions "
                "WHERE workspace_id=%s AND id=%s AND principal_id=%s",
                (context.workspace_id, session_id, context.principal_id),
            ).fetchone()
            if not existing or existing["title"] != title:
                raise RevisionConflictError("Chat session ID already allocated")
            return {**self._summary(existing), "created": False}

    def list_sessions(self, context: WorkspaceContext, *, limit: int = 50) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise InvalidInputError("Invalid chat history page size")
        with self.db.transaction(context) as conn:
            rows = conn.execute(
                "SELECT id,title,revision,updated_at FROM ui_chat_sessions "
                "WHERE workspace_id=%s AND principal_id=%s "
                "ORDER BY updated_at DESC,id LIMIT %s",
                (context.workspace_id, context.principal_id, limit),
            ).fetchall()
            return [self._summary(row) for row in rows]

    @staticmethod
    def _session(conn: Any, context: WorkspaceContext, session_id: UUID, *, lock: bool = False) -> dict[str, Any]:
        clause = " FOR UPDATE" if lock else ""
        row = conn.execute(
            "SELECT id,title,revision,updated_at FROM ui_chat_sessions "
            "WHERE workspace_id=%s AND id=%s AND principal_id=%s" + clause,
            (context.workspace_id, session_id, context.principal_id),
        ).fetchone()
        if not row:
            raise ScopeDeniedError("Chat session unavailable")
        return row

    def read_session(self, context: WorkspaceContext, session_id: UUID) -> dict[str, Any]:
        with self.db.transaction(context) as conn:
            session = self._session(conn, context, session_id)
            rows = conn.execute(
                "SELECT id,ordinal,user_text,assistant_text,state,run_id,updated_at "
                "FROM ui_chat_turns WHERE workspace_id=%s AND session_id=%s "
                "AND principal_id=%s ORDER BY ordinal DESC LIMIT 40",
                (context.workspace_id, session_id, context.principal_id),
            ).fetchall()
        turns = [{"id": str(row["id"]), "ordinal": row["ordinal"],
                  "user": row["user_text"], "assistant": row["assistant_text"],
                  "state": row["state"], "runId": row["run_id"],
                  "updatedAt": row["updated_at"].isoformat()} for row in reversed(rows)]
        return {"session": self._summary(session), "turns": turns,
                "moreBefore": turns[0]["ordinal"] > 1 if turns else False}

    def start_turn(self, context: WorkspaceContext, session_id: UUID, *, request_id: UUID, text: str) -> dict[str, Any]:
        text = _text(text, 16000)
        with self.db.transaction(context) as conn:
            session = self._session(conn, context, session_id, lock=True)
            old = conn.execute(
                "SELECT id,user_text,state FROM ui_chat_turns "
                "WHERE workspace_id=%s AND session_id=%s AND principal_id=%s AND request_id=%s",
                (context.workspace_id, session_id, context.principal_id, request_id),
            ).fetchone()
            if old:
                if old["user_text"] != text:
                    raise RevisionConflictError("Request ID belongs to a different message")
                return {"id": str(old["id"]), "created": False, "state": old["state"]}
            if session["revision"] >= 200:
                raise InvalidInputError("Chat session reached its maximum length")
            pending = conn.execute(
                "SELECT 1 FROM ui_chat_turns WHERE workspace_id=%s AND session_id=%s "
                "AND state='pending' LIMIT 1",
                (context.workspace_id, session_id),
            ).fetchone()
            if pending:
                raise RevisionConflictError("A previous chat turn is still unresolved")
            # Only completed assistant turns enter subsequent inference context.
            rows = conn.execute(
                "SELECT user_text,assistant_text FROM ui_chat_turns "
                "WHERE workspace_id=%s AND session_id=%s AND principal_id=%s AND state='completed' "
                "ORDER BY ordinal DESC LIMIT 11",
                (context.workspace_id, session_id, context.principal_id),
            ).fetchall()
            messages: list[dict[str, str]] = []
            for row in reversed(rows):
                messages.extend(({"role": "user", "content": row["user_text"]},
                                 {"role": "assistant", "content": row["assistant_text"]}))
            messages.append({"role": "user", "content": text})
            # Existing chat API's prompt budget must not be exceeded.
            if sum(len(item["content"].encode("utf-8")) for item in messages) > MAX_PROMPT_BYTES:
                raise InvalidInputError("Chat context exceeds model input allowance")
            turn_id = uuid4()
            conn.execute(
                "INSERT INTO ui_chat_turns(workspace_id,id,session_id,principal_id,request_id,ordinal,user_text) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (context.workspace_id, turn_id, session_id, context.principal_id,
                 request_id, session["revision"] + 1, text),
            )
            conn.execute(
                "UPDATE ui_chat_sessions SET revision=revision+1,updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s AND principal_id=%s",
                (context.workspace_id, session_id, context.principal_id),
            )
            return {"id": str(turn_id), "created": True, "state": "pending", "messages": messages}

    def finish_turn(self, context: WorkspaceContext, session_id: UUID, turn_id: UUID, *,
                    status: str, assistant_text: str | None, run_id: str | None) -> dict[str, Any]:
        if status not in {"completed", "indeterminate"}:
            raise InvalidInputError("Chat turn must have a terminal outcome")
        if status == "completed":
            assistant_text = _text(assistant_text, 32000)
        elif assistant_text is not None:
            raise InvalidInputError("Uncertain turns must not be reported as complete")
        if run_id is not None and (not isinstance(run_id, str) or
                                   len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id)):
            raise InvalidInputError("Invalid model run identity")
        with self.db.transaction(context) as conn:
            self._session(conn, context, session_id, lock=True)
            row = conn.execute(
                "UPDATE ui_chat_turns SET state=%s,assistant_text=%s,run_id=%s, "
                "updated_at=clock_timestamp() WHERE workspace_id=%s AND session_id=%s "
                "AND principal_id=%s AND id=%s AND state='pending' RETURNING id,state",
                (status, assistant_text, run_id, context.workspace_id, session_id,
                 context.principal_id, turn_id),
            ).fetchone()
            if not row:
                old = conn.execute(
                    "SELECT state,assistant_text,run_id FROM ui_chat_turns WHERE workspace_id=%s "
                    "AND session_id=%s AND principal_id=%s AND id=%s",
                    (context.workspace_id, session_id, context.principal_id, turn_id),
                ).fetchone()
                if not old or old["state"] != status or old["assistant_text"] != assistant_text or old["run_id"] != run_id:
                    raise RevisionConflictError("Chat turn outcome already finalized or unavailable")
            conn.execute(
                "UPDATE ui_chat_sessions SET updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s AND principal_id=%s",
                (context.workspace_id, session_id, context.principal_id),
            )
            return {"id": str(turn_id), "state": status}

    def reconcile_pending(self, context: WorkspaceContext, session_id: UUID, turn_id: UUID) -> dict[str, Any]:
        """Operator acknowledgement after possible dispatch and lease expiry.

        Never assume an unfinished inference was unbilled, and never replay it.
        The six-minute guard exceeds the BFF's five-minute model timeout.
        """
        with self.db.transaction(context) as conn:
            self._session(conn, context, session_id, lock=True)
            row = conn.execute(
                "UPDATE ui_chat_turns SET state='indeterminate',updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND session_id=%s AND id=%s AND principal_id=%s "
                "AND state='pending' AND created_at < clock_timestamp()-interval '6 minutes' "
                "RETURNING id,state",
                (context.workspace_id, session_id, turn_id, context.principal_id),
            ).fetchone()
            if row is None:
                raise RevisionConflictError("Turn is still active, complete, or unavailable")
            return {"id": str(turn_id), "state": "indeterminate", "replayed": False}


def register_chat_routes(app: FastAPI, database: Database, authenticate: Any,
                         allowed_origins: tuple[str, ...]) -> None:
    """Authenticated routes, strictly scoped by database role and workspace RLS."""
    store = UiChatStore(database)

    def actor(request: Request) -> WorkspaceContext:
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            raise ScopeDeniedError("Untrusted chat API origin")
        return authenticate(request.headers.get("authorization"))

    async def document(request: Request) -> dict[str, Any]:
        chunks, total = [], 0
        async for block in request.stream():
            total += len(block)
            if total > 50000:
                raise InvalidInputError("Chat request body exceeds its limit")
            chunks.append(block)
        try:
            value = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeError, ValueError):
            raise InvalidInputError("Chat document is not valid JSON") from None
        if not isinstance(value, dict):
            raise InvalidInputError("Chat request requires a JSON object")
        return value

    @app.get("/v1/ui/sessions")
    async def list_sessions(request: Request) -> dict[str, Any]:
        return {"items": store.list_sessions(actor(request))}

    @app.post("/v1/ui/sessions", status_code=201)
    async def create_session(request: Request) -> dict[str, Any]:
        context = actor(request)
        data = await document(request)
        if set(data) != {"id", "title"}:
            raise InvalidInputError("Expected session ID and title")
        return store.create_session(context, session_id=_uuid(data["id"]), title=data["title"])

    @app.get("/v1/ui/sessions/{session_id}")
    async def read_session(request: Request, session_id: str) -> dict[str, Any]:
        return store.read_session(actor(request), _uuid(session_id))

    @app.post("/v1/ui/sessions/{session_id}/turns", status_code=202)
    async def reserve_turn(request: Request, session_id: str) -> dict[str, Any]:
        context = actor(request)
        data = await document(request)
        if set(data) != {"requestId", "text"}:
            raise InvalidInputError("Expected idempotent request ID and text")
        return store.start_turn(context, _uuid(session_id), request_id=_uuid(data["requestId"]), text=data["text"])

    @app.post("/v1/ui/sessions/{session_id}/turns/{turn_id}/finish")
    async def finish_turn(request: Request, session_id: str, turn_id: str) -> dict[str, Any]:
        context = actor(request)
        data = await document(request)
        if set(data) != {"status", "assistant", "runId"}:
            raise InvalidInputError("Expected complete or indeterminate turn status")
        return store.finish_turn(context, _uuid(session_id), _uuid(turn_id),
                                 status=data["status"], assistant_text=data["assistant"], run_id=data["runId"])

    @app.post("/v1/ui/sessions/{session_id}/turns/{turn_id}/reconcile")
    async def reconcile_chat_turn(request: Request, session_id: str, turn_id: str) -> dict[str, Any]:
        context = actor(request)
        body = await document(request)
        if set(body) != {"acknowledgeUncertain"} or body["acknowledgeUncertain"] is not True:
            raise InvalidInputError("Explicit acknowledgement is needed to reconcile a pending turn")
        return store.reconcile_pending(context, _uuid(session_id), _uuid(turn_id))
