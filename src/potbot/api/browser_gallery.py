"""Read-only browser evidence routes on the existing authenticated Potbot API.

Never serve an arbitrary artifact ID or filesystem path supplied by the caller.
The gallery index resolves an invocation under the authenticated principal and
verifies the S37 manifest and immutable bytes before returning any content.
"""
from __future__ import annotations

from collections.abc import Callable
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError
from potbot.tools.browser_gallery import BrowserEvidenceGallery

_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'none'"}


def _integer(raw: str | None, default: int, maximum: int) -> int:
    if raw is None:
        return default
    if len(raw) > 16 or not raw.isascii() or not raw.isdecimal():
        raise InvalidInputError("browser gallery cursor/limit must be an unsigned integer")
    value = int(raw)
    if not 1 <= value <= maximum:
        raise InvalidInputError("browser gallery cursor/limit is outside its bound")
    return value


def install_gallery_routes(
    app: FastAPI, authenticate_request: Callable[[Request], WorkspaceContext],
    gallery: BrowserEvidenceGallery,
) -> None:
    if not isinstance(gallery, BrowserEvidenceGallery):
        raise InvalidInputError("browser gallery must be a qualified operator-owned service")

    @app.get("/v1/browser/evidence")
    async def browser_evidence(request: Request) -> JSONResponse:
        actor = authenticate_request(request)
        params = request.query_params.multi_items()
        if len(params) != len(dict(params)) or any(k not in {"before", "limit"} for k, _ in params):
            raise InvalidInputError("browser gallery query contains unsupported or duplicate fields")
        count = _integer(request.query_params.get("limit"), 20, 50)
        before_raw = request.query_params.get("before")
        before = _integer(before_raw, 1, 2**53 - 1) if before_raw is not None else None
        page = await run_in_threadpool(gallery.list, actor, before=before, limit=count)
        return JSONResponse({"schema_version": 1, "request_id": str(uuid4()),
                             "data": page, "error": None}, headers=_HEADERS)

    @app.get("/v1/browser/evidence/{invocation_id}/{part}")
    async def browser_evidence_part(request: Request, invocation_id: str, part: str) -> Response:
        actor = authenticate_request(request)
        if request.query_params:
            raise InvalidInputError("browser evidence read does not accept URL parameters")
        try:
            parsed = UUID(invocation_id)
            if str(parsed) != invocation_id:
                raise ValueError()
        except (ValueError, AttributeError):
            raise InvalidInputError("browser evidence ID must be a canonical UUID") from None
        if part not in {"observation", "image"}:
            raise InvalidInputError("unknown browser evidence format")
        data = await run_in_threadpool(gallery.read, actor, invocation_id, part)
        return Response(data, media_type="image/jpeg" if part == "image" else "application/json",
                        headers=_HEADERS)
