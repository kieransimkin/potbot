"""Operator-owned, revision-pinned, read-only snapshots of context documents.

Snapshots are outside every guest mount. Guests can read them through individual
Bubblewrap read-only file binds; updates go through the authenticated DB service.
"""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from potbot.context.model import ROLES, sha256, validate_content
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError

if TYPE_CHECKING:
    from potbot.context.service import WorkspaceContextService


@dataclass(frozen=True, slots=True)
class ContextProjection:
    workspace_id: UUID
    directory: Path
    revisions: tuple[int, int, int]
    digests: tuple[str, str, str]

    def inspect(self, *, excluded_roots: tuple[Path, ...] = ()) -> tuple[Path, Path, Path]:
        """Revalidate source bytes and filesystem boundaries before each launch."""
        base = Path(self.directory)
        if not base.is_absolute() or base.is_symlink():
            raise ScopeDeniedError("Context snapshot must be an absolute real directory")
        try:
            root = base.resolve(strict=True)
            meta = os.stat(root, follow_symlinks=False)
        except OSError as exc:
            raise ScopeDeniedError("Context snapshot is unavailable") from exc
        if not stat.S_ISDIR(meta.st_mode) or meta.st_mode & 0o022:
            raise ScopeDeniedError("Context snapshot directory must not be guest writable")
        if not isinstance(self.workspace_id, UUID) or len(self.revisions) != 3 or len(self.digests) != 3:
            raise InvalidInputError("Context snapshot metadata is malformed")
        for excluded in excluded_roots:
            resolved = Path(excluded).resolve(strict=True)
            if root == resolved or root.is_relative_to(resolved) or resolved.is_relative_to(root):
                raise ScopeDeniedError("Context snapshot overlaps a sandbox mount")
        files: list[Path] = []
        for role, revision, expected in zip(ROLES, self.revisions, self.digests, strict=True):
            if type(revision) is not int or revision < 1 or not isinstance(expected, str):
                raise InvalidInputError("Context snapshot revision/digest is invalid")
            path = root / role
            fd = -1
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_mode & 0o222 or info.st_size > 1_048_576):
                    raise ScopeDeniedError("Context source must be a read-only regular file")
                data = bytearray()
                while len(data) <= 1_048_576:
                    block = os.read(fd, min(65536, 1_048_577 - len(data)))
                    if not block:
                        break
                    data.extend(block)
                if len(data) != info.st_size or hashlib.sha256(data).hexdigest() != expected:
                    raise StateMismatchError("Context snapshot differs from the database revision")
            except OSError as exc:
                raise ScopeDeniedError("Context snapshot contains unsafe file") from exc
            finally:
                if fd >= 0:
                    os.close(fd)
            files.append(path)
        return files[0], files[1], files[2]


class ContextProjectionManager:
    """Operator-side snapshot generation and lifetime management.

    Capture is one database snapshot query, never three independent reads. The
    manager must remain open until all guests mounting its snapshots have exited.
    """

    def __init__(self, *, service: WorkspaceContextService, principal: WorkspaceContext,
                 parent: Path) -> None:
        if not isinstance(principal, WorkspaceContext):
            raise ScopeDeniedError("Context snapshot needs an authenticated principal")
        directory = Path(parent)
        if not directory.is_absolute() or directory.is_symlink():
            raise InvalidInputError("Snapshot parent must be an existing real absolute path")
        try:
            details = directory.stat(follow_symlinks=False)
        except OSError as exc:
            raise ScopeDeniedError("Snapshot parent is unavailable") from exc
        if not stat.S_ISDIR(details.st_mode) or details.st_mode & 0o022:
            raise ScopeDeniedError("Snapshot parent cannot be group/world writable")
        self.service = service
        self.principal = principal
        self.parent = directory.resolve(strict=True)
        self._owned: list[Path] = []
        self._closed = False

    def capture(self) -> ContextProjection:
        if self._closed:
            raise StateMismatchError("Snapshot manager is closed")
        rows = self.service.read_triple(self.principal, "sandbox", str(self.principal.workspace_id))
        directory = Path(tempfile.mkdtemp(prefix="potbot-context-", dir=self.parent))
        self._owned.append(directory)  # Track even a partial write for later cleanup.
        revisions: list[int] = []
        hashes: list[str] = []
        for role in ROLES:
            item = rows[role]
            content = item["content"]
            validate_content(content)
            checksum = sha256(content)
            if (checksum != item["sha256"] or type(item["revision"]) is not int
                    or item["revision"] < 1):
                raise StateMismatchError("Context database digest or revision is inconsistent")
            data = content.encode("utf-8")
            fd = os.open(directory / role, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o400)
            try:
                with os.fdopen(fd, "wb", closefd=False) as output:
                    output.write(data)
                    output.flush()
                os.fchmod(fd, 0o444)
                os.fsync(fd)
            finally:
                os.close(fd)
            revisions.append(item["revision"])
            hashes.append(checksum)
        rootfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(rootfd)
        finally:
            os.close(rootfd)
        projection = ContextProjection(
            self.principal.workspace_id, directory,
            (revisions[0], revisions[1], revisions[2]),
            (hashes[0], hashes[1], hashes[2]),
        )
        projection.inspect()
        return projection

    def close(self) -> None:
        """Called only after the operator confirms all guest processes exited."""
        if self._closed:
            return
        for directory in reversed(self._owned):
            if directory.is_symlink():
                raise ScopeDeniedError("Snapshot directory was redirected")
            for role in ROLES:
                path = directory / role
                if path.is_symlink():
                    raise ScopeDeniedError("Snapshot file was redirected")
                if path.exists():
                    path.unlink()
            directory.rmdir()
        self._owned.clear()
        self._closed = True
