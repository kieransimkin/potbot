"""Durable, operator-private *index* for existing browser evidence artifacts.

The ArtifactStore and SYN work journal remain authoritative. This SQLite index
only provides bounded discoverability; every read rechecks artifact content and
manifest scope. Never locate this file inside a guest sandbox/mount.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError

_ID = re.compile(r"[a-f0-9]{32}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_MAX_JPEG = 600_000
_MAX_OBSERVATION = 250_000


def _uid(raw: Any) -> str:
    if not isinstance(raw, str):
        raise InvalidInputError("browser gallery requires canonical UUID strings")
    try:
        identifier = UUID(raw)
        if str(identifier) != raw:
            raise ValueError("noncanonical UUID")
        return str(identifier)
    except (TypeError, ValueError, AttributeError) as error:
        raise InvalidInputError("browser gallery requires canonical UUIDs") from error


def _hex(raw: Any) -> str:
    if not isinstance(raw, str) or not _SHA.fullmatch(raw):
        raise InvalidInputError("browser gallery requires lower-case SHA-256")
    return raw


class BrowserEvidenceGallery:
    def __init__(self, path: Path, artifacts: Any, *, forbidden_roots: tuple[Path, ...]) -> None:
        if not callable(getattr(artifacts, "read", None)):
            raise ScopeDeniedError("browser gallery requires the authoritative artifact reader")
        path = Path(path).absolute()
        if not path.name.endswith(".sqlite3") or path.is_symlink():
            raise ScopeDeniedError("gallery index must be an operator-owned SQLite file")
        parent = path.parent
        if parent.is_symlink() or not parent.is_dir():
            raise ScopeDeniedError("gallery index needs an existing private parent")
        stat_parent = parent.stat(follow_symlinks=False)
        if stat_parent.st_uid != os.getuid() or stat.S_IMODE(stat_parent.st_mode) & 0o077:
            raise ScopeDeniedError("gallery parent must be mode 0700 and operator-owned")
        resolved = parent.resolve(strict=True)
        for root in forbidden_roots:
            root_path = Path(root).resolve(strict=True)
            if resolved == root_path or resolved.is_relative_to(root_path):
                raise ScopeDeniedError("browser gallery must never live in a guest filesystem")
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        st = path.lstat()
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) & 0o077):
            raise ScopeDeniedError("browser gallery index has unsafe owner, mode or link count")
        self.path, self.artifacts = path, artifacts
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS browser_evidence_index (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                invocation_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                manifest_id TEXT NOT NULL,
                manifest_sha TEXT NOT NULL,
                observation_id TEXT NOT NULL,
                observation_sha TEXT NOT NULL,
                image_id TEXT,
                image_sha TEXT,
                created_ns INTEGER NOT NULL,
                UNIQUE(workspace_id, invocation_id)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS browser_evidence_scope ON "
                       "browser_evidence_index(workspace_id, principal_id, seq)")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _artifact(self, actor: WorkspaceContext, artifact_id: str,
                  digest: str, limit: int) -> bytes:
        raw = self.artifacts.read(actor, UUID(_uid(artifact_id)))
        if not isinstance(raw, bytes) or len(raw) > limit or hashlib.sha256(raw).hexdigest() != digest:
            raise StateMismatchError("browser artifact bytes do not match the indexed digest")
        return raw

    def append(self, actor: WorkspaceContext, scope: Any, receipt: dict[str, Any]) -> None:
        """Called by trusted S37 recorder only, after immutable artifacts exist."""
        if not isinstance(actor, WorkspaceContext) or scope.workspace_id != actor.workspace_id or (
            scope.principal_id != actor.principal_id
        ):
            raise ScopeDeniedError("cannot register browser evidence outside its principal")
        if not isinstance(receipt, dict) or receipt.get("status") != "recorded":
            raise InvalidInputError("gallery requires a completed private evidence receipt")
        session = scope.session_id
        if not isinstance(session, str) or not _ID.fullmatch(session) or receipt.get("session_id") != session:
            raise ScopeDeniedError("browser evidence session changed")
        group = receipt.get("kind")
        if group not in {"observation", "action", "capture"}:
            raise InvalidInputError("invalid indexed evidence kind")
        manifest_id = _uid(receipt["manifest_artifact_id"])
        manifest_sha = _hex(receipt["manifest_sha256"])
        observation_id = _uid(receipt["observation_artifact_id"])
        observation_sha = _hex(receipt["observation_sha256"])
        image_id = receipt.get("image_artifact_id")
        image_sha = receipt.get("image_sha256")
        if (image_id is None) != (image_sha is None):
            raise InvalidInputError("browser image artifact identity is incomplete")
        if image_id is not None:
            image_id, image_sha = _uid(image_id), _hex(image_sha)
        manifest = json.loads(self._artifact(actor, manifest_id, manifest_sha, 8192))
        if (not isinstance(manifest, dict) or manifest.get("workspace_id") != str(actor.workspace_id)
                or manifest.get("principal_id") != str(actor.principal_id)
                or manifest.get("session_id") != session
                or manifest.get("invocation_id") != str(scope.invocation_id)
                or manifest.get("kind") != group
                or manifest.get("observation") != {"id": observation_id, "sha256": observation_sha}
                or manifest.get("image") != (
                    {"id": image_id, "sha256": image_sha} if image_id is not None else None
                ) or manifest.get("sensitive") is not True):
            raise ScopeDeniedError("browser evidence manifest scope or references do not match")
        self._artifact(actor, observation_id, observation_sha, _MAX_OBSERVATION)
        if image_id is not None:
            self._artifact(actor, image_id, image_sha, _MAX_JPEG)
        created_ns = manifest.get("captured_unix_ns")
        if type(created_ns) is not int or created_ns <= 0:
            raise InvalidInputError("evidence capture timestamp is invalid")
        with self._lock, self._connect() as db:
            try:
                db.execute("""INSERT INTO browser_evidence_index (
                    workspace_id,principal_id,invocation_id,session_id,kind,
                    manifest_id,manifest_sha,observation_id,observation_sha,
                    image_id,image_sha,created_ns) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    str(actor.workspace_id), str(actor.principal_id), str(scope.invocation_id),
                    session, group, manifest_id, manifest_sha, observation_id, observation_sha,
                    image_id, image_sha, created_ns,
                ))
            except sqlite3.IntegrityError as error:
                raise StateMismatchError("duplicate browser invocation cannot be indexed twice") from error

    def _row(self, actor: WorkspaceContext, invocation_id: str) -> sqlite3.Row:
        uid = _uid(invocation_id)
        with self._connect() as db:
            row = db.execute("""SELECT * FROM browser_evidence_index
                WHERE workspace_id=? AND principal_id=? AND invocation_id=?""",
                (str(actor.workspace_id), str(actor.principal_id), uid)).fetchone()
        if row is None:
            raise ScopeDeniedError("browser evidence does not belong to this principal")
        return row

    def list(self, actor: WorkspaceContext, *, before: int | None = None,
             limit: int = 20) -> dict[str, Any]:
        if not isinstance(actor, WorkspaceContext) or type(limit) is not int or not 1 <= limit <= 50:
            raise InvalidInputError("gallery listing requires a bounded authorized page")
        if before is not None and (type(before) is not int or not 1 <= before <= 2**53 - 1):
            raise InvalidInputError("gallery cursor must be a positive integer")
        sql = ("SELECT * FROM browser_evidence_index WHERE workspace_id=? AND principal_id=?"
               + (" AND seq<?" if before is not None else "") + " ORDER BY seq DESC LIMIT ?")
        values = (str(actor.workspace_id), str(actor.principal_id))
        with self._connect() as db:
            rows = db.execute(sql, (*values, *([before] if before is not None else []), limit + 1)).fetchall()
        has_more = len(rows) > limit
        items = [dict(row) for row in rows[:limit]]
        return {"schemaVersion": 1, "items": [{
            "id": row["invocation_id"], "sessionId": row["session_id"],
            "kind": row["kind"], "capturedUnixNs": str(row["created_ns"]),
            "manifestSha256": row["manifest_sha"], "hasImage": row["image_id"] is not None,
            "observationSha256": row["observation_sha"],
        } for row in items], "nextCursor": items[-1]["seq"] if has_more and items else None}

    def read(self, actor: WorkspaceContext, invocation_id: str, part: str) -> bytes:
        if part not in {"observation", "image"}:
            raise InvalidInputError("unknown gallery evidence part")
        row = self._row(actor, invocation_id)
        manifest = json.loads(self._artifact(actor, row["manifest_id"], row["manifest_sha"], 8192))
        if (manifest.get("workspace_id") != str(actor.workspace_id)
                or manifest.get("principal_id") != str(actor.principal_id)
                or manifest.get("session_id") != row["session_id"]
                or manifest.get("invocation_id") != invocation_id):
            raise ScopeDeniedError("browser evidence manifest is not authorized")
        if part == "image":
            if row["image_id"] is None:
                raise ScopeDeniedError("browser evidence has no captured image")
            if manifest.get("image") != {"id": row["image_id"], "sha256": row["image_sha"]}:
                raise StateMismatchError("image manifest differs from indexed artifact")
            raw = self._artifact(actor, row["image_id"], row["image_sha"], _MAX_JPEG)
            if not raw.startswith(b"\xff\xd8\xff") or not raw.endswith(b"\xff\xd9"):
                raise StateMismatchError("browser image is not valid JPEG framing")
            return raw
        if manifest.get("observation") != {"id": row["observation_id"], "sha256": row["observation_sha"]}:
            raise StateMismatchError("observation manifest differs from indexed artifact")
        return self._artifact(actor, row["observation_id"], row["observation_sha"], _MAX_OBSERVATION)
