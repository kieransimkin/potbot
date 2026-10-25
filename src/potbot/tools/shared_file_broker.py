"""Host-mediated, exact-file CAS writes inside a workspace /home.

This is one qualified path for narrow concurrent edits. General Bash/CLI
processes remain exclusively leased because they can write arbitrary /home
paths. This module never follows symlink entries or trusts model-selected
absolute host paths. The host must bind workspace_id/actor_id to authorization.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable
from uuid import UUID, uuid4

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools.agent_homes import AgentHomeRegistry
from potbot.tools.agent_permissions import (
    SimulatedAgentCredentials,
    posix_mode_allows,
)
from potbot.tools.concurrency_profiles import file_footprint
from potbot.tools.workspace_concurrency import AccessRequest, WorkspaceLeaseLedger


@dataclass(frozen=True, slots=True)
class FileWriteReceipt:
    guest_path: str
    before_sha256: str | None
    after_sha256: str
    lease_generation: int
    byte_count: int


class SharedFileBroker:
    """Serialize same-file writes; keep reads and edits to disjoint paths parallel.

    The caller must provide an explicit old SHA or None to mean 'must not exist'.
    The method never overwrites data after an unexpected external change.
    A moved/unconfirmed file effect stays indeterminate until reconciled.
    """

    def __init__(
        self, *, ledger: WorkspaceLeaseLedger,
        authorize: Callable[[UUID, UUID], bool], max_file_bytes: int = 4 * 1024**2,
        agent_homes: AgentHomeRegistry | None = None,
    ) -> None:
        if not callable(authorize):
            raise ScopeDeniedError("shared file access requires host authorization")
        if type(max_file_bytes) is not int or not 1 <= max_file_bytes <= 128 * 1024**2:
            raise InvalidInputError("invalid file access limit")
        self.ledger = ledger
        self.authorize = authorize
        self.max_file_bytes = max_file_bytes
        self.agent_homes = agent_homes

    def _require(self, workspace_id: UUID, actor_id: UUID, image_digest: str) -> None:
        if self.authorize(workspace_id, actor_id) is not True:
            raise ScopeDeniedError("actor is not authorized for this workspace")
        pin = self.ledger.image_status(workspace_id)
        if pin is None or pin["digest"] != image_digest:
            raise StateMismatchError("workspace image revision changed")

    def _open_parent(self, path: str) -> tuple[int, str]:
        # The AccessResource parser also requires canonical /home paths.
        canonical = file_footprint("file_edit", (path,)).resources[0].name
        parts = PurePosixPath(canonical).parts
        if len(parts) < 4:
            raise InvalidInputError("write must be within a workspace subdirectory")
        current = os.open(self.ledger.home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in parts[2:-1]:
                next_fd = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current,
                )
                os.close(current)
                current = next_fd
            return current, parts[-1]
        except BaseException:
            os.close(current)
            raise

    def _check_agent_directory_permissions(
        self, *, workspace_id: UUID, actor_id: UUID, guest_path: str,
    ) -> None:
        """Simulated group/UID checks for a trusted, mediated write.

        These checks do NOT make arbitrary guest shell writes secure: the host
        filesystem must also enforce distinct kernel credentials, or restrict
        write effects to broker-mediated file operations.
        """
        if self.agent_homes is None:
            return
        relative = PurePosixPath(guest_path).parts[2:]
        actor_home = self.agent_homes.lookup(workspace_id, actor_id)
        if actor_home is None:
            raise ScopeDeniedError("writer has no registered agent identity")
        owner = self.agent_homes.owner_for_directory(workspace_id, relative[0])
        if owner is None:
            raise ScopeDeniedError("file path is not under a registered agent directory")
        credentials = SimulatedAgentCredentials(actor_home.uid, actor_home.gid,
                                                 actor_home.group_name)
        current = self.ledger.home
        # A writer needs (w,x) in the *parent* directory and (x) in every
        # ancestor. An existing file with mode 0644 may still be replaced under
        # a group-writable, non-sticky 0775 parent, matching POSIX rename rules.
        for index, component in enumerate(relative[:-1]):
            current = current / component
            info = os.stat(current, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise ScopeDeniedError("agent path has a non-directory parent")
            for bit in (("write", "execute") if index == len(relative) - 2 else ("execute",)):
                if not posix_mode_allows(
                    mode=stat.S_IMODE(info.st_mode), owner_uid=owner.uid,
                    group_gid=owner.gid, actor=credentials, permission=bit,
                ):
                    raise ScopeDeniedError("agent directory mode denies shared file mutation")
            if info.st_mode & stat.S_ISVTX and actor_home.uid != owner.uid:
                raise ScopeDeniedError("sticky agent directory blocks peer replacement")

    @staticmethod
    def _existing(fd: int, name: str, *, max_bytes: int) -> str | None:
        try:
            opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=fd)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(opened)
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                raise ScopeDeniedError("file is not a bounded regular file")
            digest = hashlib.sha256()
            total = 0
            while chunk := os.read(opened, 64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ScopeDeniedError("file changed beyond the supported bound")
                digest.update(chunk)
            return digest.hexdigest()
        finally:
            os.close(opened)

    def replace(
        self, *, workspace_id: UUID, actor_id: UUID, image_digest: str,
        guest_path: str, expected_sha256: str | None, data: bytes,
        idempotency_key: str,
    ) -> FileWriteReceipt:
        if not isinstance(data, bytes) or len(data) > self.max_file_bytes:
            raise InvalidInputError("file bytes exceed the bounded write limit")
        if expected_sha256 is not None and (
            not isinstance(expected_sha256, str) or len(expected_sha256) != 64
            or any(char not in "0123456789abcdef" for char in expected_sha256)
        ):
            raise InvalidInputError("expected file SHA-256 is not canonical")
        self._require(workspace_id, actor_id, image_digest)
        footprint = file_footprint("file_edit", (guest_path,))
        self._check_agent_directory_permissions(
            workspace_id=workspace_id, actor_id=actor_id, guest_path=guest_path,
        )
        request = AccessRequest(
            workspace_id, actor_id, "file.atomic_replace", idempotency_key,
            "write", footprint.resources, image_digest,
        )
        lease = self.ledger.claim(request, replay_existing=False)
        parent_fd = -1
        temp_name = ".potbot-stage-" + uuid4().hex
        physical_effect = False
        try:
            parent_fd, leaf = self._open_parent(guest_path)
            current = self._existing(parent_fd, leaf, max_bytes=self.max_file_bytes)
            if current != expected_sha256:
                raise StateMismatchError("file differs from expected immutable input")
            # Staged name is unique, owned by this broker, and never supplied by
            # the agent. The protected parent must already exist.
            created_fd = os.open(
                temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=parent_fd,
            )
            try:
                with os.fdopen(created_fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                physical_effect = True  # cleanup may require reconciliation
            # Defend against an unbrokered concurrent actor changing the file
            # between preparation and commit. This check alone cannot defeat a
            # racing rogue shell; only enforced mediation can guarantee that.
            if self._existing(parent_fd, leaf, max_bytes=self.max_file_bytes) != expected_sha256:
                raise StateMismatchError("file changed while preparing atomic replacement")
            os.chmod(temp_name, 0o644, dir_fd=parent_fd, follow_symlinks=False)
            os.replace(temp_name, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
            digest = hashlib.sha256(data).hexdigest()
            self.ledger.finish(lease, result_sha256=digest)
            return FileWriteReceipt(guest_path, current, digest, lease.generation, len(data))
        except BaseException:
            # The new file could have been published before an exception was
            # raised (e.g. fsync). Never announce a successful final state then.
            try:
                self.ledger.finish(lease, result_sha256=hashlib.sha256(
                    b"atomic-file-operation-unresolved" if physical_effect
                    else b"file-write-preflight-rejected").hexdigest(), uncertain=physical_effect)
            except StateMismatchError:
                pass
            raise
        finally:
            if parent_fd >= 0:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                os.close(parent_fd)
