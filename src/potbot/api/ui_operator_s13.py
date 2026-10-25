"""S13: authenticated, status-only operator projection for the existing UI.

The browser receives no controller path, lease token, backend credential,
process command, private telemetry, or privileged operator RPC surface.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import CapabilityUnavailableError, ScopeDeniedError
from potbot.tools.operator_attachment_s13 import OperatorAttachmentClient

if TYPE_CHECKING:
    from fastapi import FastAPI
    from potbot.persistence.database import Database

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


def _configured_client(workspace_id: UUID) -> OperatorAttachmentClient | None:
    path = os.environ.get("POTBOT_OPERATOR_SOCKET")
    if not path:
        if os.environ.get("POTBOT_OPERATOR_REQUIRED") == "1":
            raise CapabilityUnavailableError("Managed operator endpoint is not configured")
        return None
    variables = (
        os.environ.get("POTBOT_OPERATOR_IMAGE_DIGEST"),
        os.environ.get("POTBOT_OPERATOR_QUALIFICATION_SHA256"),
        os.environ.get("POTBOT_OPERATOR_MOUNTS_SHA256"),
    )
    if (os.environ.get("POTBOT_OPERATOR_WORKSPACE_ID") != str(workspace_id)
            or any(not isinstance(v, str) or not _SHA.fullmatch(v) for v in variables)):
        raise CapabilityUnavailableError("Managed operator has no verified pins")
    return OperatorAttachmentClient(
        socket_path=Path(path), workspace_id=workspace_id,
        image_digest=variables[0], qualification_sha256=variables[1],
        mounts_sha256=variables[2],
    )


def register_operator_routes(
    app: FastAPI, database: Database, authenticate: Callable[[str | None], WorkspaceContext],
    allowed_origins: tuple[str, ...], workspace_id: UUID,
) -> None:
    # This client survives requests and refuses epoch changes in a still-live
    # API process; the native service must restart children after owner failure.
    client = _configured_client(workspace_id)

    @app.get("/v1/ui/operator")
    async def operator_status(request: Request) -> JSONResponse:
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            raise ScopeDeniedError("Browser origin unavailable")
        if request.query_params:
            raise ScopeDeniedError("Operator status does not accept query parameters")
        actor = authenticate(request.headers.get("authorization"))
        if actor.workspace_id != workspace_id:
            raise ScopeDeniedError("Operator state is outside authenticated workspace")
        # The RLS transaction validates current membership even if the socket
        # status was recently cached by this process.
        with database.transaction(actor):
            pass
        if client is None:
            return JSONResponse({"schema": "potbot-ui-operator/v1", "workspaceId": str(workspace_id),
                    "configured": False, "healthy": False, "journal": "unavailable",
                    "releaseGate": "unknown", "pendingReleases": None,
                    "activeRuns": None, "epoch": None}, headers=_HEADERS)
        try:
            status = await run_in_threadpool(client.require_healthy)
        except (CapabilityUnavailableError, ScopeDeniedError):
            return JSONResponse({"schema": "potbot-ui-operator/v1", "workspaceId": str(workspace_id),
                    "configured": True, "healthy": False, "journal": "unavailable",
                    "releaseGate": "unknown", "pendingReleases": None,
                    "activeRuns": None, "epoch": None}, headers=_HEADERS)
        return JSONResponse({"schema": "potbot-ui-operator/v1", "workspaceId": str(workspace_id),
                "configured": True, "healthy": True, "journal": "healthy",
                "releaseGate": "observed" if status["pending_release_gates"] is not None else "unknown",
                "pendingReleases": status["pending_release_gates"],
                "activeRuns": status["supervised_runs"], "epoch": status["epoch"]},
                headers=_HEADERS)
