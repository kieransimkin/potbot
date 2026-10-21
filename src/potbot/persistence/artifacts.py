"""Content-addressed artifacts with scoped metadata and atomic local publication.

Filesystem bytes are published before the SQL metadata transaction commits. A
rollback can leave unreferenced bytes, but can never expose an incomplete object
through the service. A future garbage collector must retain referenced objects.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid4, uuid5

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError

if TYPE_CHECKING:
    from potbot.persistence.database import Database

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ARTIFACT_NAMESPACE = UUID("ca6e6652-05d4-5d38-986d-22fb6ef12a41")


class ArtifactIntegrityError(InvalidInputError):
    """Stored bytes, declared identity, or a locator failed validation."""


@dataclass(frozen=True, slots=True)
class PublishedArtifact:
    digest: str
    byte_length: int
    locator: str


class ArtifactBackend(Protocol):
    name: str

    def publish(self, workspace_id: UUID, data: bytes) -> PublishedArtifact: ...

    def read(self, workspace_id: UUID, artifact: PublishedArtifact) -> bytes: ...


class LocalArtifactBackend:
    """Local immutable objects; path components are opened without symlink traversal."""

    name = "local-v1"

    def __init__(self, root: Path, *, max_bytes: int = 64 * 1024 * 1024) -> None:
        if type(max_bytes) is not int or max_bytes < 1:
            raise InvalidInputError("Artifact size limit must be a positive integer.")
        self.root = Path(root).absolute()
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ArtifactIntegrityError("Artifact root must be a real directory.")

    @staticmethod
    def locator(workspace_id: UUID, digest: str) -> str:
        if not isinstance(workspace_id, UUID) or not _DIGEST.fullmatch(digest):
            raise ArtifactIntegrityError("Invalid artifact identity.")
        return f"{workspace_id}/sha256/{digest[:2]}/{digest}"

    @contextmanager
    def _directory(self, locator: str, *, create: bool) -> Iterator[tuple[int, str]]:
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = locator.split("/")
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                        os.fsync(descriptor)
                    except FileExistsError:
                        pass
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = child
            yield descriptor, parts[-1]
        finally:
            os.close(descriptor)

    def publish(self, workspace_id: UUID, data: bytes) -> PublishedArtifact:
        if not isinstance(data, bytes) or len(data) > self.max_bytes:
            raise InvalidInputError("Artifact payload must be bytes within the size limit.")
        digest = hashlib.sha256(data).hexdigest()
        artifact = PublishedArtifact(digest, len(data), self.locator(workspace_id, digest))
        try:
            with self._directory(artifact.locator, create=True) as (directory, filename):
                temporary = f".pending-{uuid4().hex}"
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    try:
                        os.link(
                            temporary,
                            filename,
                            src_dir_fd=directory,
                            dst_dir_fd=directory,
                            follow_symlinks=False,
                        )
                    except FileExistsError:
                        pass  # Verify the existing object below; never overwrite it.
                    os.fsync(directory)
                finally:
                    os.unlink(temporary, dir_fd=directory)
                    os.fsync(directory)
            self.read(workspace_id, artifact)
            return artifact
        except OSError as error:
            raise ArtifactIntegrityError("Artifact could not be published safely.") from error

    def read(self, workspace_id: UUID, artifact: PublishedArtifact) -> bytes:
        expected = self.locator(workspace_id, artifact.digest)
        if (
            artifact.locator != expected
            or type(artifact.byte_length) is not int
            or not 0 <= artifact.byte_length <= self.max_bytes
        ):
            raise ArtifactIntegrityError("Artifact locator or size failed validation.")
        try:
            with self._directory(expected, create=False) as (directory, filename):
                descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                with os.fdopen(descriptor, "rb") as stream:
                    metadata = os.fstat(stream.fileno())
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_size != artifact.byte_length
                    ):
                        raise ArtifactIntegrityError("Artifact size or file type has changed.")
                    data = stream.read(artifact.byte_length + 1)
        except OSError as error:
            raise ArtifactIntegrityError("Artifact could not be read safely.") from error
        if len(data) != artifact.byte_length or hashlib.sha256(data).hexdigest() != artifact.digest:
            raise ArtifactIntegrityError("Artifact content digest does not match.")
        return data


class ArtifactStore:
    def __init__(self, db: Database, backend: ArtifactBackend) -> None:
        self.db = db
        self.backend = backend

    def put(
        self, context: WorkspaceContext, data: bytes, media_type: str = "application/octet-stream"
    ) -> dict[str, Any]:
        with self.db.transaction(context) as connection:
            return self.put_in_transaction(connection, context, data, media_type)

    def put_in_transaction(
        self,
        connection: Any,
        context: WorkspaceContext,
        data: bytes,
        media_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        if not isinstance(media_type, str) or not 1 <= len(media_type) <= 255:
            raise InvalidInputError("Artifact media type must be a nonempty string.")
        published = self.backend.publish(context.workspace_id, data)
        identity = uuid5(_ARTIFACT_NAMESPACE, f"{context.workspace_id}:{published.digest}")
        connection.execute(
            """INSERT INTO artifacts
            (workspace_id,id,digest,byte_length,media_type,backend,locator)
            VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (workspace_id,digest) DO NOTHING""",
            (
                context.workspace_id,
                identity,
                published.digest,
                published.byte_length,
                media_type,
                self.backend.name,
                published.locator,
            ),
        )
        row = connection.execute(
            "SELECT * FROM artifacts WHERE workspace_id=%s AND digest=%s",
            (context.workspace_id, published.digest),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("Resource is unavailable in this workspace.")
        if row["byte_length"] != published.byte_length or row["backend"] != self.backend.name:
            raise ArtifactIntegrityError("Existing artifact metadata conflicts with publication.")
        return dict(row)

    def metadata(self, context: WorkspaceContext, artifact_id: UUID) -> dict[str, Any]:
        with self.db.transaction(context) as connection:
            return self._metadata(connection, context, artifact_id)

    @staticmethod
    def _metadata(connection: Any, context: WorkspaceContext, artifact_id: UUID) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM artifacts WHERE workspace_id=%s AND id=%s",
            (context.workspace_id, artifact_id),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("Resource is unavailable in this workspace.")
        return dict(row)

    def read(self, context: WorkspaceContext, artifact_id: UUID) -> bytes:
        with self.db.transaction(context) as connection:
            return self.read_in_transaction(connection, context, artifact_id)

    def read_in_transaction(
        self, connection: Any, context: WorkspaceContext, artifact_id: UUID
    ) -> bytes:
        row = self._metadata(connection, context, artifact_id)
        if row["backend"] != self.backend.name:
            raise ArtifactIntegrityError("Artifact storage backend is not available.")
        return self.backend.read(
            context.workspace_id,
            PublishedArtifact(row["digest"], row["byte_length"], row["locator"]),
        )
