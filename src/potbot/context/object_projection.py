"""Shared registered-object context views beneath the existing read-only library.

Principal-private contexts remain RPC-only and are NEVER copied into the shared
mount. The catalogue is advisory. It does not grant write or execution rights.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.context.catalogue import list_objects
from potbot.context.model import ROLES, sha256, validate_content, validate_identity
from potbot.context.service import WorkspaceContextService
from potbot.context.sessions import _BoundTransaction
from potbot.context.skill_projection import SkillLibraryProjector, _directory, _json
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.tools.temporary import TemporaryDirectory, allocate_file


class ObjectContextProjector(SkillLibraryProjector):
    def __init__(self, root: Path) -> None:
        # Reuse the qualified custody/atomic-file helpers, not the skill namespace.
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise InvalidInputError("Object projection requires Linux/WSL")
        self.root = _directory(Path(root))
        self.library = _directory(self.root / "_objects", create=True)

    def _object_snapshot(self, item: dict[str, Any], docs: dict[str, Any]) -> dict[str, Any]:
        identity = UUID(item["id"])
        validate_identity(item["kind"], item["name"])
        if item["visibility"] != "shared" or set(docs) != set(ROLES):
            raise InvalidInputError("Only complete shared objects can be projected")
        files = {}
        revisions = {}
        for role in ROLES:
            row = docs[role]
            validate_content(row["content"])
            if (
                type(row["revision"]) is not int
                or row["revision"] < 1
                or sha256(row["content"]) != row["sha256"]
            ):
                raise StateMismatchError("Object context integrity mismatch")
            files[role] = row["content"].encode()
            revisions[role] = {"revision": row["revision"], "sha256": row["sha256"]}
        metadata = {
            "schema": "potbot-object-context-view/v1",
            "object_id": str(identity),
            "kind": item["kind"],
            "name": item["name"],
            "context": revisions,
            "instructions_authoritative": False,
        }
        raw = _json(metadata)
        digest = hashlib.sha256(raw).hexdigest()
        files["object.json"] = raw
        folder = _directory(self.library / identity.hex, create=True)
        target = folder / digest
        if not target.exists() and not target.is_symlink():
            with TemporaryDirectory(prefix=".object-", dir=folder) as temporary:
                staging = Path(temporary)
                for name, data in files.items():
                    self._write(staging, name, data)
                staging.chmod(0o555)
                fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.rename(staging, target)
                fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        self._verify(target, files)
        return {
            **metadata,
            "snapshot_sha256": digest,
            "path": target.relative_to(self.root).as_posix(),
        }

    def sync(
        self,
        service: WorkspaceContextService,
        context: WorkspaceContext,
        *,
        max_objects: int = 4096,
    ) -> dict[str, Any]:
        if type(max_objects) is not int or not 1 <= max_objects <= 16384:
            raise InvalidInputError("Invalid object inventory bound")
        with self._lock():
            entries = []
            after = None
            count = total = 0
            while True:
                with service.database.transaction(context) as conn:
                    page = list_objects(conn, context, after=after, limit=100)
                    bound = WorkspaceContextService(_BoundTransaction(context, conn))
                    for item in page["objects"]:
                        count += 1
                        if count > max_objects:
                            raise StateMismatchError("Object inventory exceeds projection budget")
                        if item["visibility"] != "shared":
                            continue
                        docs = bound.read_triple(context, item["kind"], item["name"])
                        total += sum(len(doc["content"].encode()) for doc in docs.values())
                        if total > 64 * 1024**2:
                            raise StateMismatchError(
                                "Object context exceeds projection byte budget"
                            )
                        entries.append(self._object_snapshot(item, docs))
                if page["next"] is None:
                    break
                after = UUID(page["next"])
            catalogue = {"schema": "potbot-object-catalogue/v1", "objects": entries}
            raw = _json(catalogue)
            if len(raw) > 8 * 1024**2:
                raise StateMismatchError("Object catalogue exceeds its byte budget")
            target = self.library / "catalogue.json"
            if target.exists() or target.is_symlink():
                # Same no-follow, owner, mode and bounded read checks as snapshots.
                import stat

                fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    info = os.fstat(fd)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.geteuid()
                        or info.st_nlink != 1
                        or info.st_mode & 0o222
                        or info.st_size > 8 * 1024**2
                    ):
                        raise StateMismatchError("Object catalogue custody changed")
                    with os.fdopen(fd, "rb", closefd=False) as stream:
                        if stream.read(len(raw) + 1) == raw:
                            return catalogue
                finally:
                    os.close(fd)
            fd, temporary = allocate_file(self.library, prefix=".catalogue-")
            try:
                with os.fdopen(fd, "wb", closefd=False) as stream:
                    stream.write(raw)
                    stream.flush()
                os.fchmod(fd, 0o444)
                os.fsync(fd)
                os.replace(temporary, target)
                parent_fd = os.open(self.library, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            finally:
                os.close(fd)
                temporary.unlink(missing_ok=True)
            return catalogue
