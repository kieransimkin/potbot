"""Operator-only materialization of the PostgreSQL authored-skill catalogue.

All published directories are immutable. A small advisory catalogue selects
current context snapshots; it is not authorization to load/execute a skill.
Old snapshots are retained while a worker may still hold a mounted reference.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.context.model import ROLES, sha256, validate_content
from potbot.context.service import WorkspaceContextService
from potbot.context.skill_catalogue import load_skill, published_skills, skill_identity
from potbot.context.skills import validate_skill
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools.temporary import TemporaryDirectory, allocate_file


def _directory(path: Path, *, create: bool = False) -> Path:
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
        raise ScopeDeniedError("Skill projection needs a real operator-controlled path")
    if create:
        path.mkdir(mode=0o755, exist_ok=True)
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ScopeDeniedError("Skill projection directory has unsafe custody")
    return path


def prepare_skill_root(parent: Path, workspace_id: UUID) -> Path:
    """Default beneath the explicit controller directory, never inside a guest home."""
    if not isinstance(workspace_id, UUID):
        raise InvalidInputError("Skill root requires a workspace UUID")
    parent = _directory(Path(parent))
    return _directory(parent / ("skills-" + workspace_id.hex), create=True)


def _json(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()


class SkillProjectionBusy(StateMismatchError):
    """Another trusted publisher holds the short-lived local projection lock."""


class _ProjectionCustody:
    """Shared custody checks for immutable, operator-owned projections."""

    root: Path
    library: Path

    @contextmanager
    def _lock(self) -> Iterator[None]:
        import fcntl

        _directory(self.root)
        _directory(self.library)
        fd = os.open(
            self.library / ".projection.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
            ):
                raise ScopeDeniedError("Skill projection lock custody changed")
            # Never stall the operator health loop behind another projector.
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SkillProjectionBusy("Skill projection is already running") from None
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _write(folder: Path, name: str, data: bytes) -> None:
        fd = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
            os.fchmod(fd, 0o444)
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _verify(folder: Path, files: dict[str, bytes]) -> None:
        _directory(folder)
        if folder.stat().st_mode & 0o222 or {p.name for p in folder.iterdir()} != set(files):
            raise ScopeDeniedError("Immutable skill snapshot was replaced")
        for name, expected in files.items():
            fd = os.open(folder / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                    or info.st_mode & 0o222
                    or info.st_size != len(expected)
                ):
                    raise ScopeDeniedError("Skill snapshot file is mutable or linked")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    if stream.read(len(expected) + 1) != expected:
                        raise StateMismatchError("Skill snapshot integrity mismatch")
            finally:
                os.close(fd)


class SkillLibraryProjector(_ProjectionCustody):
    """Single-host, locked publication under /usr/local/skills/_agents.

    PostgreSQL is authoritative. flock coordinates projections only, never
    database writes or distributed worker admission. There is no SQLite store.
    """

    def __init__(self, root: Path) -> None:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise ScopeDeniedError("Skill projection requires the qualified Linux/WSL operator")
        self.root = _directory(Path(root))
        self.library = _directory(self.root / "_agents", create=True)

    def _snapshot(self, context: WorkspaceContext, document: dict[str, Any]) -> dict[str, Any]:
        try:
            namespace = document["namespace"]
            agent_id = UUID(hex=namespace.removeprefix("agent-"))
            identity, _, name = skill_identity(context.workspace_id, agent_id, document["name"])
            if (
                namespace != "agent-" + agent_id.hex
                or str(identity) != document["skill_id"]
                or name != document["context_name"]
            ):
                raise ValueError
            validate_skill(document["name"], document["content"])
            if sha256(document["content"]) != document["sha256"]:
                raise ValueError
            docs = document["context"]
            if set(docs) != set(ROLES):
                raise ValueError
            files = {"SKILL.md": document["content"].encode("utf-8")}
            revisions = {}
            for role in ROLES:
                row = docs[role]
                validate_content(row["content"])
                if (
                    type(row["revision"]) is not int
                    or row["revision"] < 1
                    or sha256(row["content"]) != row["sha256"]
                ):
                    raise ValueError
                files[role] = row["content"].encode("utf-8")
                revisions[role] = {"revision": row["revision"], "sha256": row["sha256"]}
            manifest = {
                "schema": "potbot-authored-skill-view/v1",
                "skill_id": str(identity),
                "release_sha256": document["sha256"],
                "context": revisions,
                "trusted": False,
                "automatically_activated": False,
            }
            manifest_raw = _json(manifest)
            snapshot_id = hashlib.sha256(manifest_raw).hexdigest()
            files["release.json"] = manifest_raw
        except (KeyError, TypeError, ValueError, AttributeError):
            raise InvalidInputError("Invalid published skill projection") from None
        author = _directory(self.library / agent_id.hex, create=True)
        slug = _directory(author / document["name"], create=True)
        target = slug / snapshot_id
        if not target.exists() and not target.is_symlink():
            with TemporaryDirectory(prefix=".publishing-", dir=slug) as temporary:
                folder = Path(temporary)
                for role, data in files.items():
                    self._write(folder, role, data)
                folder.chmod(0o555)
                directory_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                os.rename(folder, target)  # create-only; lock excludes concurrent projectors
                self._verify(target, files)
                directory_fd = os.open(slug, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        else:
            self._verify(target, files)
        return {
            "skill_id": str(identity),
            "release_sha256": document["sha256"],
            "snapshot_sha256": snapshot_id,
            "path": target.relative_to(self.root).as_posix(),
            "context": revisions,
        }

    def sync(
        self, service: WorkspaceContextService, context: WorkspaceContext, *, max_skills: int = 256
    ) -> dict[str, Any]:
        if type(max_skills) is not int or not 1 <= max_skills <= 512:
            raise InvalidInputError("Invalid skill projection bound")
        from potbot.context.sessions import _BoundTransaction

        with self._lock():
            entries: list[dict[str, Any]] = []
            after: UUID | None = None
            total_bytes = 0
            while True:
                with service.database.transaction(context) as conn:
                    page = published_skills(conn, context, limit=25, after=after)
                    for item in page["skills"]:
                        if len(entries) >= max_skills:
                            raise StateMismatchError("Skill catalogue exceeds projection bound")
                        view = load_skill(
                            conn,
                            context,
                            WorkspaceContextService(_BoundTransaction(context, conn)),
                            UUID(item["skill_id"]),
                            item["release_sha256"],
                        )
                        total_bytes += len(view["content"].encode()) + sum(
                            len(row["content"].encode()) for row in view["context"].values()
                        )
                        if total_bytes > 64 * 1024 * 1024:
                            raise StateMismatchError("Skill catalogue exceeds byte allowance")
                        entries.append(self._snapshot(context, view))
                if page["next"] is None:
                    break
                after = UUID(page["next"])
            raw = _json(
                {
                    "schema": "potbot-authored-skills/v1",
                    "workspace_id": str(context.workspace_id),
                    "skills": entries,
                    "trusted": False,
                    "automatically_activated": False,
                }
            )
            index = self.library / "catalogue.json"
            if index.is_symlink():
                raise ScopeDeniedError("Skill catalogue index was redirected")
            if index.exists():
                info = index.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                    or info.st_mode & 0o222
                    or info.st_size > 2 * 1024 * 1024
                ):
                    raise ScopeDeniedError("Skill catalogue index custody changed")
            if index.exists() and index.read_bytes() == raw:
                return {
                    "changed": False,
                    "skills": len(entries),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            fd, pending = allocate_file(self.library, prefix=".catalogue-")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                    os.fchmod(stream.fileno(), 0o444)
                os.replace(pending, index)
                parent_fd = os.open(self.library, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            finally:
                pending.unlink(missing_ok=True)
            return {
                "changed": True,
                "skills": len(entries),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
