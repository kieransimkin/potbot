"""Authenticated workspace UI preferences and checksum-pinned model transfers.

The UI never grants new credentials or selects arbitrary output paths. Transfers
are queued and executed by an explicitly configured, separate worker, not by
model inference or an HTTP request handler.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from fastapi import Request
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError

if TYPE_CHECKING:
    from fastapi import FastAPI
    from potbot.persistence.database import Database

# Preferences are deliberately non-secret appearance settings, never executable
# configuration, credentials, endpoints or access-control overrides.
PREFERENCES = {
    "theme": {"system", "dark", "light"},
    "graphDisplay": {"3d", "list"},
}
BOOL_PREFERENCES = frozenset(("reducedMotion", "showGraphDiagnostics", "orbAutoHide", "downloadNotifications"))
_QUANTS = frozenset(("Q2_K", "Q3_K_M", "Q4_K_M", "Q5_K_M", "Q6_K", "Q8_0", "F16", "BF16"))
_HF_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,199}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_REV = re.compile(r"[0-9a-f]{40}\Z")
_MAX_BYTES = 128 * 1024**3


def validate_preferences(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) > 12:
        raise InvalidInputError("Preferences must be an object of approved UI settings")
    if set(value) - set(PREFERENCES) - BOOL_PREFERENCES - {"xpraMaxFps"}:
        raise InvalidInputError("Unknown preference; private and executable settings are forbidden")
    result: dict[str, Any] = {}
    for key, selection in value.items():
        if key in PREFERENCES:
            if type(selection) is not str or selection not in PREFERENCES[key]:
                raise InvalidInputError("Invalid enumerated UI preference")
        elif key in BOOL_PREFERENCES:
            if type(selection) is not bool:
                raise InvalidInputError("UI preference must be a boolean")
        elif key == "xpraMaxFps":
            if type(selection) is not int or selection not in {10, 15, 24, 30, 60}:
                raise InvalidInputError("Unsupported Xpra render frame limit")
        result[key] = selection
    return result


@dataclass(frozen=True, slots=True)
class TransferArtifact:
    model_id: str
    artifact_repository: str
    revision: str
    filename: str
    quantization: str
    sha256: str
    size_bytes: int

    @classmethod
    def parse(cls, raw: object) -> TransferArtifact:
        expected = {"model_id", "artifact_repository", "revision", "filename",
                    "quantization", "sha256", "size_bytes"}
        if not isinstance(raw, dict) or set(raw) != expected:
            raise InvalidInputError("An immutable model artifact manifest is required")
        try:
            result = cls(**raw)
        except TypeError:
            raise InvalidInputError("Invalid artifact manifest fields") from None
        if (type(result.model_id) is not str or not _MODEL.fullmatch(result.model_id)
                or type(result.artifact_repository) is not str or not _HF_REPO.fullmatch(result.artifact_repository)
                or type(result.filename) is not str or not _FILENAME.fullmatch(result.filename)
                or type(result.revision) is not str or not _REV.fullmatch(result.revision)
                or type(result.sha256) is not str or not _SHA.fullmatch(result.sha256)
                or type(result.quantization) is not str or result.quantization not in _QUANTS
                or type(result.size_bytes) is not int or not 0 < result.size_bytes <= _MAX_BYTES):
            raise InvalidInputError("Artifact needs a pinned revision, exact size, hash and safe name")
        if result.filename.endswith(".gguf"):
            pass
        elif result.filename.endswith(".safetensors") and result.quantization in {"BF16", "F16"}:
            pass
        else:
            raise InvalidInputError("Quantization and artifact filename format mismatch")
        return result


class UiResourceStore:
    def __init__(self, database: Database) -> None:
        self.db = database

    def workspace(self, context: WorkspaceContext) -> dict[str, Any]:
        with self.db.transaction(context) as conn:
            row = conn.execute("SELECT id,name FROM workspaces WHERE id=%s",
                               (context.workspace_id,)).fetchone()
            if row is None:
                raise ScopeDeniedError("Workspace unavailable")
            return {"id": str(row["id"]), "name": row["name"]}

    def preferences(self, context: WorkspaceContext) -> dict[str, Any]:
        with self.db.transaction(context) as conn:
            row = conn.execute(
                "SELECT revision,settings FROM ui_preferences WHERE workspace_id=%s AND principal_id=%s",
                (context.workspace_id, context.principal_id),
            ).fetchone()
        return {"revision": int(row["revision"]) if row else 0,
                "settings": validate_preferences(row["settings"] if row else {})}

    def update_preferences(self, context: WorkspaceContext, revision: int, settings: object) -> dict[str, Any]:
        if type(revision) is not int or not 0 <= revision <= 2**53:
            raise InvalidInputError("Invalid preferences revision")
        values = validate_preferences(settings)
        from psycopg.types.json import Jsonb

        with self.db.transaction(context) as conn:
            if revision == 0:
                row = conn.execute(
                    "INSERT INTO ui_preferences(workspace_id,principal_id,revision,settings) "
                    "VALUES (%s,%s,1,%s) ON CONFLICT DO NOTHING RETURNING revision",
                    (context.workspace_id, context.principal_id, Jsonb(values)),
                ).fetchone()
                if row is not None:
                    return {"revision": 1, "settings": values}
            row = conn.execute(
                "UPDATE ui_preferences SET revision=revision+1,settings=%s,updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND principal_id=%s AND revision=%s RETURNING revision",
                (Jsonb(values), context.workspace_id, context.principal_id, revision),
            ).fetchone()
            if row is None:
                raise RevisionConflictError("Preferences changed in another session")
            return {"revision": row["revision"], "settings": values}

    def add_transfer(self, context: WorkspaceContext, *, request_id: UUID,
                     artifact: TransferArtifact) -> dict[str, Any]:
        with self.db.transaction(context) as conn:
            # Serialize admission within the workspace to preserve a real disk
            # allowance even when multiple browser sessions submit concurrently.
            conn.execute("SELECT pg_catalog.pg_advisory_xact_lock(hashtext(%s))",
                         ("potbot:ui:transfers:" + str(context.workspace_id),))
            existing = conn.execute(
                "SELECT * FROM ui_artifact_transfers WHERE workspace_id=%s AND principal_id=%s "
                "AND idempotency_key=%s",
                (context.workspace_id, context.principal_id, request_id),
            ).fetchone()
            if existing is not None:
                if any(existing[key] != val for key, val in (
                    ("model_id", artifact.model_id), ("artifact_repository", artifact.artifact_repository),
                    ("revision", artifact.revision), ("filename", artifact.filename),
                    ("quantization", artifact.quantization), ("sha256", artifact.sha256),
                    ("expected_bytes", artifact.size_bytes),
                )):
                    raise RevisionConflictError("Idempotency key identifies a different artifact")
                return {"id": str(existing["id"]), "state": existing["state"], "created": False}
            quota = conn.execute(
                "SELECT COUNT(*) FILTER (WHERE state IN ('queued','downloading','verifying')) AS active, "
                "COALESCE(SUM(expected_bytes) FILTER (WHERE state IN "
                "('queued','downloading','verifying','installed')),0) AS reserved "
                "FROM ui_artifact_transfers WHERE workspace_id=%s",
                (context.workspace_id,),
            ).fetchone()
            if quota["active"] >= 12 or quota["reserved"] + artifact.size_bytes > 256 * 1024**3:
                raise InvalidInputError("Workspace transfer queue or disk allowance is exhausted")
            ident = uuid4()
            row = conn.execute(
                "INSERT INTO ui_artifact_transfers "
                "(workspace_id,id,principal_id,idempotency_key,model_id,artifact_repository,revision,filename,"
                "quantization,sha256,expected_bytes) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (workspace_id,principal_id,idempotency_key) DO NOTHING RETURNING id",
                (context.workspace_id, ident, context.principal_id, request_id,
                 artifact.model_id, artifact.artifact_repository, artifact.revision,
                 artifact.filename, artifact.quantization, artifact.sha256, artifact.size_bytes),
            ).fetchone()
            if row:
                return {"id": str(row["id"]), "state": "queued", "created": True}
            existing = conn.execute(
                "SELECT * FROM ui_artifact_transfers WHERE workspace_id=%s AND principal_id=%s "
                "AND idempotency_key=%s",
                (context.workspace_id, context.principal_id, request_id),
            ).fetchone()
            if existing is None or any(existing[key] != val for key, val in (
                ("model_id", artifact.model_id), ("artifact_repository", artifact.artifact_repository),
                ("revision", artifact.revision), ("filename", artifact.filename),
                ("quantization", artifact.quantization), ("sha256", artifact.sha256),
                ("expected_bytes", artifact.size_bytes),
            )):
                raise RevisionConflictError("Idempotency key identifies a different artifact")
            return {"id": str(existing["id"]), "state": existing["state"], "created": False}

    @staticmethod
    def public_transfer(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": str(row["id"]), "modelId": row["model_id"],
                "revision": row["revision"], "filename": row["filename"],
                "quantization": row["quantization"], "state": row["state"],
                "expectedBytes": row["expected_bytes"],
                "downloadedBytes": row["downloaded_bytes"],
                "verifiedBytes": row["verified_bytes"],
                "cancelRequested": row["cancel_requested"],
                "errorCode": row["error_code"],
                "updatedAt": row["updated_at"].isoformat()}

    def transfers(self, context: WorkspaceContext, *, limit: int = 50) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidInputError("Invalid transfer page size")
        with self.db.transaction(context) as conn:
            rows = conn.execute(
                "SELECT * FROM ui_artifact_transfers WHERE workspace_id=%s AND principal_id=%s "
                "ORDER BY created_at DESC,id LIMIT %s",
                (context.workspace_id, context.principal_id, limit),
            ).fetchall()
        return [self.public_transfer(row) for row in rows]

    def cancel_transfer(self, context: WorkspaceContext, ident: UUID) -> dict[str, Any]:
        with self.db.transaction(context) as conn:
            row = conn.execute(
                "UPDATE ui_artifact_transfers SET cancel_requested=true, "
                "state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END, "
                "updated_at=clock_timestamp() WHERE workspace_id=%s AND principal_id=%s "
                "AND id=%s AND state IN ('queued','downloading','verifying') "
                "RETURNING id,state,cancel_requested",
                (context.workspace_id, context.principal_id, ident),
            ).fetchone()
        if row is None:
            raise ScopeDeniedError("Transfer unavailable or already terminal")
        return {"id": str(row["id"]), "state": row["state"], "cancelRequested": True}


def register_ui_routes(app: FastAPI, database: Database, authenticate: Any,
                       allowed_origins: tuple[str, ...]) -> None:
    """Install backend routes without broadening the existing bearer-token scope."""
    store = UiResourceStore(database)

    def actor(request: Request) -> WorkspaceContext:
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            raise ScopeDeniedError("Browser origin unavailable")
        return authenticate(request.headers.get("authorization"))

    async def parse(request: Request) -> Any:
        if int(request.headers.get("content-length") or "0") > 4096:
            raise InvalidInputError("UI request exceeds limit")
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 4096:
                raise InvalidInputError("UI request exceeds limit")
            chunks.append(chunk)
        try:
            return json.loads(b"".join(chunks).decode("utf-8"))
        except (ValueError, UnicodeError):
            raise InvalidInputError("Invalid UI JSON document") from None

    @app.get("/v1/ui/workspace")
    async def workspace(request: Request) -> dict[str, Any]:
        return {"workspace": store.workspace(actor(request)), "available": True}

    @app.get("/v1/ui/preferences")
    async def preferences(request: Request) -> dict[str, Any]:
        return store.preferences(actor(request))

    @app.put("/v1/ui/preferences")
    async def set_preferences(request: Request) -> dict[str, Any]:
        identity = actor(request)
        value = await parse(request)
        if not isinstance(value, dict) or set(value) != {"revision", "settings"}:
            raise InvalidInputError("Expected revision and settings")
        return store.update_preferences(identity, value["revision"], value["settings"])

    @app.get("/v1/ui/downloads")
    async def downloads(request: Request) -> dict[str, Any]:
        return {"items": store.transfers(actor(request)), "transferTelemetry": True,
                "installTelemetry": True}

    @app.post("/v1/ui/downloads", status_code=202)
    async def add_download(request: Request) -> dict[str, Any]:
        identity = actor(request)
        value = await parse(request)
        if not isinstance(value, dict) or set(value) != {"requestId", "artifact", "confirm"} or value["confirm"] is not True:
            raise InvalidInputError("Explicitly confirmed immutable artifact required")
        try:
            request_id = UUID(str(value["requestId"]))
        except ValueError:
            raise InvalidInputError("Invalid transfer idempotency UUID") from None
        return store.add_transfer(identity, request_id=request_id, artifact=TransferArtifact.parse(value["artifact"]))

    @app.post("/v1/ui/downloads/{transfer_id}/cancel")
    async def cancel(request: Request, transfer_id: str) -> dict[str, Any]:
        identity = actor(request)
        async for chunk in request.stream():
            if chunk:
                raise InvalidInputError("Transfer cancellation must have no request body")
        try:
            ident = UUID(transfer_id)
        except ValueError:
            raise InvalidInputError("Invalid transfer ID") from None
        return store.cancel_transfer(identity, ident)
