"""R2.5: durable steering inbox for single-writer reasoning runs.

HTTP threads create immutable requests, never mutate the tree snapshot. Only the
reasoning owner evaluates the inbox at a checkpoint and publishes a decision.
All paths are scoped by the authenticated workspace and opaque run identity.
This is not distributed multi-writer scheduling or an external message broker.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import UUID

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.tools.temporary import allocate_file, create_directory

_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_MAX_PROMPT_BYTES = 8192


def _write_atomic(path: Path, record: dict[str, Any]) -> None:
    fd, temporary = allocate_file(path.parent, prefix=".steering-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class SteeringInbox:
    """Exactly-once decision publication for an owner-scheduled run.

    Requests are idempotent via request ID and their files are never overwritten.
    A lost evaluation before publication is safely re-evaluated only when the
    controller can establish no provider dispatch; interrupted model calls
    remain indeterminate in the tree and require operator reconciliation.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root).absolute()
        self._lock = RLock()
        self._sealed: set[tuple[UUID, str]] = set()

    def _paths(self, workspace_id: UUID, run_id: str) -> tuple[Path, Path]:
        if not isinstance(workspace_id, UUID) or not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise InvalidInputError("canonical workspace and run identity are required")
        if self.root.is_symlink():
            raise ScopeDeniedError("reasoning root is unavailable")
        workspace = self.root / str(workspace_id)
        if workspace.is_symlink():
            raise ScopeDeniedError("reasoning workspace is unavailable")
        tree_file = workspace / (run_id + ".json")
        mailbox = workspace / (run_id + ".steering")
        if mailbox.is_symlink() or not tree_file.is_file() or tree_file.is_symlink():
            raise ScopeDeniedError("reasoning run is unavailable")
        return tree_file, mailbox

    def _state(self, workspace_id: UUID, run_id: str) -> tuple[dict[str, Any], Path]:
        tree_file, mailbox = self._paths(workspace_id, run_id)
        value = json.loads(tree_file.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ScopeDeniedError("reasoning run is unavailable")
        return value, mailbox

    @staticmethod
    def _key(request_id: str) -> str:
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise InvalidInputError("steering request_id must be 1-128 safe characters")
        return hashlib.sha256(request_id.encode()).hexdigest()

    def submit(self, workspace_id: UUID, run_id: str, *, request_id: str, prompt: str) -> dict[str, Any]:
        with self._lock:
            return self._submit_locked(workspace_id, run_id, request_id=request_id, prompt=prompt)

    def _submit_locked(self, workspace_id: UUID, run_id: str, *, request_id: str, prompt: str) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
            raise InvalidInputError("steering prompt must be 1-8192 UTF-8 bytes")
        state, mailbox = self._state(workspace_id, run_id)
        key = self._key(request_id)
        # Duplicate submissions return their original decision even after a
        # run closes. A new request is rejected once synthesis is sealed.
        if mailbox.exists() and mailbox.is_symlink():
            raise ScopeDeniedError("steering inbox is unavailable")
        create_directory(mailbox)
        path = mailbox / (key + ".request.json")
        record: dict[str, Any] = {
            "id": key, "request_id": request_id, "prompt": prompt,
            "created_ns": time.time_ns(), "status": "queued",
        }
        if path.exists():
            previous = json.loads(path.read_text(encoding="utf-8"))
            if previous.get("request_id") != request_id or previous.get("prompt") != prompt:
                raise InvalidInputError("steering idempotency conflict")
            return self._result(mailbox, key, previous)
        if state.get("run_status", "running") != "running" or (workspace_id, run_id) in self._sealed:
            raise InvalidInputError("reasoning run is not accepting steering prompts")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            previous = json.loads(path.read_text(encoding="utf-8"))
            if previous.get("request_id") != request_id or previous.get("prompt") != prompt:
                raise InvalidInputError("steering idempotency conflict") from None
            return self._result(mailbox, key, previous)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        return record

    @staticmethod
    def _result(mailbox: Path, key: str, request: dict[str, Any]) -> dict[str, Any]:
        response = mailbox / (key + ".decision.json")
        return json.loads(response.read_text(encoding="utf-8")) if response.is_file() else request

    def get(self, workspace_id: UUID, run_id: str, request_id: str) -> dict[str, Any]:
        _, mailbox = self._state(workspace_id, run_id)
        key = self._key(request_id)
        path = mailbox / (key + ".request.json")
        if not path.is_file() or path.is_symlink():
            raise ScopeDeniedError("steering prompt is unavailable")
        return self._result(mailbox, key, json.loads(path.read_text(encoding="utf-8")))

    def pending(self, workspace_id: UUID, run_id: str, *, limit: int = 16) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return self._pending_locked(workspace_id, run_id, limit=limit)

    def seal(self, workspace_id: UUID, run_id: str) -> None:
        """Reject subsequent new prompts before the last decision checkpoint.

        This lock is process-local: multiple API worker processes require a
        PostgreSQL queue/lock rather than sharing the file prototype.
        """
        with self._lock:
            self._state(workspace_id, run_id)
            self._sealed.add((workspace_id, run_id))

    def _pending_locked(self, workspace_id: UUID, run_id: str, *, limit: int = 16) -> tuple[dict[str, Any], ...]:
        _, mailbox = self._state(workspace_id, run_id)
        if not mailbox.is_dir():
            return ()
        candidates: list[dict[str, Any]] = []
        for path in mailbox.glob("*.request.json"):
            if path.is_symlink() or path.stat().st_size > _MAX_PROMPT_BYTES + 1024:
                raise ScopeDeniedError("steering request is unavailable")
            key = path.name.split(".", 1)[0]
            if (mailbox / (key + ".decision.json")).is_file():
                continue
            candidates.append(json.loads(path.read_text(encoding="utf-8")))
            if len(candidates) > 64:
                raise InvalidInputError("too many unresolved steering requests")
        candidates.sort(key=lambda item: (item["created_ns"], item["id"]))
        return tuple(candidates[:limit])

    def resolve(self, workspace_id: UUID, run_id: str, request: dict[str, Any],
                decision: dict[str, Any]) -> dict[str, Any]:
        _, mailbox = self._state(workspace_id, run_id)
        key = request["id"]
        target = mailbox / (key + ".decision.json")
        if target.exists():
            return json.loads(target.read_text(encoding="utf-8"))
        result = {"id": key, "request_id": request["request_id"],
                  "prompt": request["prompt"], "created_ns": request["created_ns"],
                  "status": "decided", **decision}
        _write_atomic(target, result)
        return result

    def history(self, workspace_id: UUID, run_id: str) -> list[dict[str, Any]]:
        """Bounded chronological steering history with independent replies."""
        _, mailbox = self._state(workspace_id, run_id)
        if not mailbox.exists():
            return []
        results = []
        for path in mailbox.glob("*.request.json"):
            if path.is_symlink():
                raise ScopeDeniedError("steering record is unavailable")
            request = json.loads(path.read_text(encoding="utf-8"))
            results.append(self._result(mailbox, request["id"], request))
            if len(results) > 256:
                raise InvalidInputError("steering history exceeds page limit")
        return sorted(results, key=lambda item: (item.get("created_ns", 0), item["id"]))
