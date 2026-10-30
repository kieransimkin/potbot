"""Authenticated, workspace-scoped read and audited-restore context UI endpoints.

Installed by the API owner, never by a sandbox worker. All information and
mutation requests are checked using Potbot's PostgreSQL workspace RLS and
existing context CAS/write-grant machinery. No host paths or DB DSNs leak.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from potbot.context.inspection import ContextHistoryInspector
from potbot.context.model import validate_identity
from potbot.context.service import WorkspaceContextService
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError

if TYPE_CHECKING:
    from fastapi import FastAPI
    from potbot.persistence.database import Database

_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


def _params(request: Request, allowed: frozenset[str]) -> dict[str, str]:
    items = request.query_params.multi_items()
    if len(items) != len(set(k for k, _ in items)) or any(k not in allowed for k, _ in items):
        raise InvalidInputError("Unexpected context query parameters")
    return dict(items)


def _target(data: dict[str, Any]) -> tuple[str, str, str]:
    try:
        kind, name, role = data["kind"], data["name"], data["role"]
    except KeyError:
        raise InvalidInputError("Context target is incomplete") from None
    validate_identity(kind, name, role)
    return kind, name, role


def _integer(value: str | None, *, maximum: int, default: int | None = None) -> int:
    if value is None:
        if default is None:
            raise InvalidInputError("Context revision is required")
        return default
    if not value.isascii() or not value.isdecimal() or len(value) > 16:
        raise InvalidInputError("Invalid context revision")
    result = int(value)
    if result < 0 or result > maximum:
        raise InvalidInputError("Context revision out of range")
    return result


async def _json_small(request: Request, max_bytes: int = 8192) -> dict[str, Any]:
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise InvalidInputError("Expected JSON request")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > max_bytes:
            raise InvalidInputError("Context action is too large")

    def pairs(entries: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in entries:
            if key in value:
                raise InvalidInputError("Duplicate context field")
            value[key] = item
        return value

    try:
        decoded = json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeError, ValueError) as exc:
        raise InvalidInputError("Invalid context JSON") from exc
    if not isinstance(decoded, dict):
        raise InvalidInputError("Expected a context JSON object")
    return decoded


def register_context_routes(
    app: FastAPI, database: Database,
    authenticate: Callable[[str | None], WorkspaceContext],
    allowed_origins: tuple[str, ...], workspace_id: UUID,
) -> None:
    inspector = ContextHistoryInspector(WorkspaceContextService(database))

    def authorized(request: Request) -> WorkspaceContext:
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            raise ScopeDeniedError("Unknown browser origin")
        principal = authenticate(request.headers.get("authorization"))
        if principal.workspace_id != workspace_id:
            raise ScopeDeniedError("Context is outside authenticated workspace")
        return principal

    @app.get("/v1/ui/context/document")
    async def context_document(request: Request) -> JSONResponse:
        query = _params(request, frozenset({"kind", "name", "role", "limit"}))
        target = _target(query)
        limit = _integer(query.get("limit"), maximum=200, default=25)
        if limit < 1:
            raise InvalidInputError("History limit must be positive")
        actor = authorized(request)
        result = await run_in_threadpool(inspector.inspect, actor, *target, history_limit=limit)
        # Read the text separately but require the same revision and digest;
        # concurrent writes fail closed instead of returning a mixed snapshot.
        current = await run_in_threadpool(inspector.service.read, actor, *target)
        if (current["revision"] != result["revision"] or
                current["sha256"] != result["sha256"]):
            raise RevisionConflictError("Context changed while reading; retry")
        raw = current["content"].encode("utf-8")
        preview = raw[:262144].decode("utf-8", errors="ignore")
        return JSONResponse({"schema": "potbot-ui-context/v1", **result,
                             "content": preview, "truncated": len(raw) > 262144},
                            headers=_HEADERS)

    @app.get("/v1/ui/context/diff")
    async def context_diff(request: Request) -> JSONResponse:
        query = _params(request, frozenset({"kind", "name", "role", "before", "after"}))
        target = _target(query)
        before = _integer(query.get("before"), maximum=9_223_372_036_854_775_807)
        after = _integer(query.get("after"), maximum=9_223_372_036_854_775_807)
        if before < 1 or after < 1:
            raise InvalidInputError("Diff requires committed revisions")
        actor = authorized(request)
        result = await run_in_threadpool(inspector.diff, actor, *target, before, after)
        return JSONResponse({"schema": "potbot-ui-context-diff/v1", "diff": result}, headers=_HEADERS)

    @app.get("/v1/ui/context/events")
    async def context_events(request: Request) -> JSONResponse:
        query = _params(request, frozenset({"after", "limit"}))
        after = _integer(query.get("after"), maximum=9_223_372_036_854_775_807, default=0)
        limit = _integer(query.get("limit"), maximum=100, default=50)
        if limit < 1:
            raise InvalidInputError("Context event limit must be positive")
        actor = authorized(request)
        events = await run_in_threadpool(inspector.service.events, actor, after_id=after, limit=limit)
        return JSONResponse({"schema": "potbot-ui-context-events/v1", "events": events}, headers=_HEADERS)

    @app.post("/v1/ui/context/restore")
    async def context_restore(request: Request) -> JSONResponse:
        actor = authorized(request)
        body = await _json_small(request)
        required = {"kind", "name", "role", "source_revision", "expected_revision",
                    "idempotency_key", "confirm"}
        if set(body) not in (required, required | {"reason"}) or body["confirm"] is not True:
            raise InvalidInputError("Restore requires an exact confirmed request")
        target = _target(body)
        before, expected = body["source_revision"], body["expected_revision"]
        key = body["idempotency_key"]
        reason = body.get("reason", "operator-reviewed context restore")
        if (type(before) is not int or before < 1 or type(expected) is not int or expected < 1
                or not isinstance(key, str) or not 1 <= len(key) <= 128
                or not isinstance(reason, str) or len(reason) > 900):
            raise InvalidInputError("Invalid context restore preconditions")
        result = await run_in_threadpool(
            inspector.restore, actor, *target,
            source_revision=before, expected_revision=expected,
            idempotency_key=key, reason=reason,
        )
        return JSONResponse({"schema": "potbot-ui-context-restored/v1", **result}, headers=_HEADERS)
