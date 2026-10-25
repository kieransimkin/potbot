"""Host-owned agent identities and guarded home-directory renames.

Simulated UIDs/GID are metadata until a qualified per-agent kernel credential
profile enforces them; never claim POSIX isolation from a shared real UID.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools.agent_skeleton import install_agent_skeleton
from potbot.tools.workspace_concurrency import (
    AccessLease,
    AccessRequest,
    AccessResource,
    WorkspaceLeaseLedger,
)

_NAME = re.compile(r"[a-z][a-z0-9_-]{1,62}\Z")
_INITIAL = re.compile(r"[0-9a-f]{64}\Z")
_RESERVED = frozenset({"root", "models", "shared", "tmp", "cache", "etc", "bin", "proc", "dev", "run", "sys"})


def default_agent_directory(agent_id: UUID) -> str:
    if not isinstance(agent_id, UUID):
        raise InvalidInputError("agent home requires a verified UUID")
    return hashlib.sha256(agent_id.bytes).hexdigest()


def _name(value: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value) or value in _RESERVED:
        raise InvalidInputError("agent name must be 2-63 lowercase letters, digits, - or _")
    return value


def _rename_noreplace(fd: int, old: str, new: str) -> None:
    """Linux renameat2 is essential: a racing guest cannot replace another home."""
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "renameat2", None)
    if function is None:
        raise CapabilityUnavailableError("atomic no-replace rename requires Linux renameat2")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(fd, old.encode(), fd, new.encode(), 1) != 0:  # RENAME_NOREPLACE
        error = ctypes.get_errno()
        if error in (errno.EEXIST, errno.ENOTEMPTY):
            raise StateMismatchError("agent home destination already exists")
        raise OSError(error, os.strerror(error))


@dataclass(frozen=True, slots=True)
class AgentHome:
    workspace_id: UUID
    agent_id: UUID
    directory: str
    revision: int
    uid: int
    gid: int
    group_name: str

    @property
    def guest_path(self) -> str:
        return f"/home/{self.directory}"


class AgentHomeRegistry:
    """Controller-side mapping, concurrency-fenced against live workspace tools.

    A crash during physical rename can leave a directory name out of sync with
    the registry; lookup fails closed until a host-admin reconciles it. No guest
    process should be allowed to rename or replace the top-level agent folders.
    """

    def __init__(self, ledger: WorkspaceLeaseLedger, *, workspace_name: str) -> None:
        self.ledger = ledger
        # The group name is the human-readable workspace name converted to a
        # POSIX-safe identifier. Every workspace has its own namespace /etc/group.
        group = re.sub(r"[^a-z0-9_-]+", "-", workspace_name.strip().casefold()).strip("-")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", group):
            raise InvalidInputError("workspace name must produce a bounded group identifier")
        self.group_name = group
        self.workspace_name = workspace_name
        with ledger._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS agent_workspace_groups (
                workspace_id TEXT PRIMARY KEY, group_name TEXT NOT NULL, gid INTEGER NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS agent_homes (
                workspace_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                directory TEXT NOT NULL, revision INTEGER NOT NULL,
                uid INTEGER NOT NULL CHECK(uid BETWEEN 420 AND 65533),
                gid INTEGER NOT NULL CHECK(gid=69),
                PRIMARY KEY(workspace_id,agent_id),
                UNIQUE(workspace_id,directory), UNIQUE(workspace_id,uid)
            )""")

    def _guard(self, workspace_id: UUID, agent_id: UUID, operation: str) -> AccessLease:
        pin = self.ledger.image_status(workspace_id)
        if pin is None or not isinstance(pin["digest"], str):
            raise StateMismatchError("agent home requires a pinned workspace image")
        request = AccessRequest(
            workspace_id, agent_id, operation, uuid4().hex, "unknown",
            (AccessResource("workspace", "*", "write"),), pin["digest"],
        )
        return self.ledger.claim(request, replay_existing=False)

    def _dir_fd(self) -> int:
        home = self.ledger.home
        if home.is_symlink() or not home.is_dir():
            raise ScopeDeniedError("registered workspace /home is unavailable")
        return os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)

    @staticmethod
    def _assert_directory(fd: int, name: str) -> None:
        details = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISDIR(details.st_mode):
            raise ScopeDeniedError("agent home was replaced or is not a real directory")

    def lookup(self, workspace_id: UUID, agent_id: UUID) -> AgentHome | None:
        if not isinstance(workspace_id, UUID) or not isinstance(agent_id, UUID):
            raise InvalidInputError("agent home lookup needs workspace and agent UUIDs")
        with self.ledger._connect() as db:
            group = db.execute(
                "SELECT group_name FROM agent_workspace_groups WHERE workspace_id=?",
                (str(workspace_id),),
            ).fetchone()
            if group is not None and group["group_name"] != self.group_name:
                raise StateMismatchError("workspace group identity differs from the host registration")
            row = db.execute(
                "SELECT directory,revision,uid,gid FROM agent_homes WHERE workspace_id=? AND agent_id=?",
                (str(workspace_id), str(agent_id)),
            ).fetchone()
        if row is None:
            return None
        fd = self._dir_fd()
        try:
            self._assert_directory(fd, row["directory"])
        except FileNotFoundError as exc:
            raise StateMismatchError("agent home missing; host reconciliation required") from exc
        finally:
            os.close(fd)
        return AgentHome(workspace_id, agent_id, row["directory"], row["revision"],
                         row["uid"], row["gid"], self.group_name)

    def owner_for_directory(self, workspace_id: UUID, directory: str) -> AgentHome | None:
        """Resolve the host-owned top-level agent namespace (no guest aliases)."""
        if not isinstance(workspace_id, UUID) or not isinstance(directory, str):
            raise InvalidInputError("agent directory owner lookup is invalid")
        with self.ledger._connect() as db:
            row = db.execute(
                "SELECT agent_id FROM agent_homes WHERE workspace_id=? AND directory=?",
                (str(workspace_id), directory),
            ).fetchone()
        return self.lookup(workspace_id, UUID(row["agent_id"])) if row else None

    def provision(self, workspace_id: UUID, agent_id: UUID) -> AgentHome:
        existing = self.lookup(workspace_id, agent_id)
        if existing is not None:
            return existing
        lease = self._guard(workspace_id, agent_id, "agent.home.provision")
        name = default_agent_directory(agent_id)
        try:
            fd = self._dir_fd()
        except BaseException:
            self.ledger.finish(
                lease, result_sha256=hashlib.sha256(b"home-preflight-rejected").hexdigest()
            )
            raise
        uncertain = False
        try:
            with self.ledger._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute(
                    "SELECT directory FROM agent_homes WHERE workspace_id=? AND agent_id=?",
                    (str(workspace_id), str(agent_id)),
                ).fetchone()
                if existing is not None:
                    raise StateMismatchError("concurrent agent home provisioning")
                group = db.execute(
                    "SELECT group_name FROM agent_workspace_groups WHERE workspace_id=?",
                    (str(workspace_id),),
                ).fetchone()
                if group is not None and group["group_name"] != self.group_name:
                    raise StateMismatchError("workspace group identity changed")
                if group is None:
                    db.execute("INSERT INTO agent_workspace_groups VALUES(?,?,69)",
                               (str(workspace_id), self.group_name))
                allocated = db.execute(
                    "SELECT MAX(uid) AS last FROM agent_homes WHERE workspace_id=?",
                    (str(workspace_id),),
                ).fetchone()
                uid = max(419, allocated["last"] or 419) + 1
                if uid > 65533:
                    raise StateMismatchError("workspace agent UID capacity exhausted")
                os.mkdir(name, 0o775, dir_fd=fd)
                uncertain = True
                # umask often clears group write; set exactly the requested mode.
                os.chmod(name, 0o775, dir_fd=fd, follow_symlinks=False)
                agent_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                                   os.O_CLOEXEC, dir_fd=fd)
                try:
                    install_agent_skeleton(agent_fd, workspace_name=self.workspace_name)
                finally:
                    os.close(agent_fd)
                db.execute(
                    "INSERT INTO agent_homes VALUES(?,?,?,?,?,69)",
                    (str(workspace_id), str(agent_id), name, 1, uid),
                )
            uncertain = False
            return AgentHome(workspace_id, agent_id, name, 1, uid, 69, self.group_name)
        finally:
            os.close(fd)
            # Uncertain physical filesystem effects continue blocking further
            # mutations until reconciled, even if the database transaction failed.
            self.ledger.finish(lease, result_sha256=hashlib.sha256(name.encode()).hexdigest(),
                               uncertain=uncertain)

    def rename(
        self, workspace_id: UUID, agent_id: UUID, new_name: str, *, expected_revision: int,
    ) -> AgentHome:
        name = _name(new_name)
        if type(expected_revision) is not int or expected_revision < 1:
            raise InvalidInputError("agent home rename needs expected revision")
        lease = self._guard(workspace_id, agent_id, "agent.home.rename")
        try:
            fd = self._dir_fd()
        except BaseException:
            self.ledger.finish(
                lease, result_sha256=hashlib.sha256(b"home-preflight-rejected").hexdigest()
            )
            raise
        uncertain = False
        try:
            with self.ledger._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT directory,revision,uid,gid FROM agent_homes WHERE workspace_id=? AND agent_id=?",
                    (str(workspace_id), str(agent_id)),
                ).fetchone()
                if row is None or row["revision"] != expected_revision:
                    raise StateMismatchError("agent home rename revision is stale")
                old = row["directory"]
                self._assert_directory(fd, old)
                if old == name:
                    raise StateMismatchError("agent home already has this name")
                if db.execute(
                    "SELECT 1 FROM agent_homes WHERE workspace_id=? AND directory=?",
                    (str(workspace_id), name),
                ).fetchone():
                    raise StateMismatchError("another agent has reserved that name")
                _rename_noreplace(fd, old, name)
                uncertain = True
                db.execute(
                    "UPDATE agent_homes SET directory=?, revision=? "
                    "WHERE workspace_id=? AND agent_id=?",
                    (name, expected_revision + 1, str(workspace_id), str(agent_id)),
                )
            uncertain = False
            return AgentHome(workspace_id, agent_id, name, expected_revision + 1,
                             row["uid"], row["gid"], self.group_name)
        finally:
            os.close(fd)
            self.ledger.finish(lease, result_sha256=hashlib.sha256(name.encode()).hexdigest(),
                               uncertain=uncertain)
