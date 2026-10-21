"""Private controller receipts bridge process completion and database commit."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import canonical_json, content_hash


class RunReceiptStore:
    """An operator-owned directory never mounted into a repository process.

    Receipts record observed, fully stopped invocations. They are not evidence of
    completion when missing, and no PID is reused as authority to kill a process.
    """

    def __init__(self, root: Path) -> None:
        original = root.absolute()
        if original.resolve() != original:
            raise ScopeDeniedError("Receipt root cannot traverse symlinks.")
        original.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not original.is_dir() or original.is_symlink():
            raise ScopeDeniedError("Receipt root must be a controller-owned directory.")
        self.root = original

    def _directory(self, workspace_id: UUID) -> Path:
        if not isinstance(workspace_id, UUID):
            raise InvalidInputError("Receipt workspace must be a UUID.")
        path = self.root / str(workspace_id)
        if path.is_symlink():
            raise ScopeDeniedError("Receipt workspace must not be a symlink.")
        path.mkdir(mode=0o700, exist_ok=True)
        return path

    def write(self, workspace_id: UUID, run_id: UUID, receipt: dict[str, Any]) -> str:
        if receipt.get("run_id") != str(run_id) or receipt.get("workspace_id") != str(workspace_id):
            raise InvalidInputError("Receipt identity does not match its destination.")
        raw = canonical_json(receipt).encode()
        if len(raw) > 48 * 1024**2:
            raise InvalidInputError("Receipt exceeds its bounded capture size.")
        directory = self._directory(workspace_id)
        target = directory / f"{run_id}.json"
        temporary = directory / f".{run_id}.{uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError:
                existing = self.read(workspace_id, run_id)
                if existing != receipt:
                    raise StateMismatchError(
                        "A different completion receipt already exists."
                    ) from None
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        return content_hash(receipt)

    def read(self, workspace_id: UUID, run_id: UUID) -> dict[str, Any] | None:
        target = self._directory(workspace_id) / f"{run_id}.json"
        try:
            descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError:
            raise ScopeDeniedError("Receipt cannot be opened safely.") from None
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(48 * 1024**2 + 1)
        if len(raw) > 48 * 1024**2:
            raise InvalidInputError("Stored receipt exceeds its supported bound.")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            raise InvalidInputError("Stored completion receipt is invalid.") from None
        if (
            not isinstance(value, dict)
            or value.get("run_id") != str(run_id)
            or value.get("workspace_id") != str(workspace_id)
        ):
            raise InvalidInputError("Stored completion receipt identity is invalid.")
        if canonical_json(value).encode() != raw:
            raise InvalidInputError("Stored receipt is not in its canonical format.")
        return value
