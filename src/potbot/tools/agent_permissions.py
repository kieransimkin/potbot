"""Logical POSIX identities/modes for mediated workspace operations.

These checks are NOT a kernel security boundary. If the guest maps all simulated
UIDs to the same host UID, chmod(0755) does not protect one agent from another.
An unrestricted terminal must remain workspace-exclusive until the host qualifies
real per-agent UID mapping or an enforcing filesystem mediator.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from typing import Literal

from potbot.domain.errors import InvalidInputError

WORKSPACE_GID = 69
FIRST_AGENT_UID = 420
DEFAULT_DIRECTORY_MODE = 0o775
DEFAULT_FILE_MODE = 0o644
COLLABORATIVE_FILE_MODE = 0o664
SHARED_HOME_STICKY_MODE = 0o1775
Permission = Literal["read", "write", "execute"]


@dataclass(frozen=True, slots=True)
class SimulatedAgentCredentials:
    uid: int
    gid: int
    group_name: str

    def __post_init__(self) -> None:
        if type(self.uid) is not int or not FIRST_AGENT_UID <= self.uid <= 65533:
            raise InvalidInputError("agent UID must be allocated from 420")
        if self.gid != WORKSPACE_GID:
            raise InvalidInputError("workspace agents must belong to group 69")
        if not self.group_name:
            raise InvalidInputError("workspace group needs an explicit name")


def posix_mode_allows(
    *, mode: int, owner_uid: int, group_gid: int,
    actor: SimulatedAgentCredentials, permission: Permission,
) -> bool:
    """For already-mediated accesses only; no symlink or sticky-dir bypass.

    Directory writes also require traversal (execute) on ancestor directories.
    A directory mode 0755 blocks creating/replacing child entries by peers, but
    does not prevent writing an existing group-writable file in that directory.
    """
    if type(mode) is not int or not 0 <= mode <= 0o7777:
        raise InvalidInputError("invalid POSIX mode")
    if permission not in {"read", "write", "execute"}:
        raise InvalidInputError("invalid requested permission")
    bit = {"read": 4, "write": 2, "execute": 1}[permission]
    shift = 6 if actor.uid == owner_uid else 3 if actor.gid == group_gid else 0
    return bool((mode >> shift) & bit)


def safe_agent_rename_allowed(
    *, parent_mode: int, owner_uid: int, actor_uid: int,
    entry_owner_uid: int,
) -> bool:
    """POSIX sticky-bit behavior for top-level /home entry operations."""
    if type(parent_mode) is not int:
        raise InvalidInputError("invalid parent mode")
    return not (parent_mode & stat.S_ISVTX) or actor_uid in {owner_uid, entry_owner_uid}
