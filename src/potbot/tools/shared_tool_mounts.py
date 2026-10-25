"""Host-only admission for one writable tool mount in the shared workspace.

Normal workspaces mount /usr/local/tools read-only, even though its backing
store is deliberately group-writable. Only an active, renewable, exclusive
per-tool maintenance lease can temporarily make ONE selected installation
writable inside a qualified installer sandbox. Neither this grant nor a
filesystem permission bit can authorize a tool installation by itself.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools.agent_homes import AgentHome
from potbot.tools.workspace_concurrency import AccessResource

if TYPE_CHECKING:
    from potbot.tools.shared_workspace_isolation import SharedWorkspaceRuntime
    from potbot.tools.workspace_supervision import WorkspaceLeaseGuard

_TOOL = re.compile(r"[a-z][a-z0-9-]{1,62}\Z")
_AGENT = re.compile(r"(?:[a-z][a-z0-9_-]{1,62}|[0-9a-f]{64})\Z")


@dataclass(frozen=True, slots=True)
class ToolWriteGrant:
    """A host-issued capability tied to a live maintenance lease and agent ID.

    The controller must obtain AgentHome from AgentHomeRegistry.lookup and
    must never deserialize this object from model, CLI or HTTP request data.
    The lease guard is NOT passed into the guest process or environment.
    """

    slug: str
    agent: AgentHome
    guard: WorkspaceLeaseGuard

    def admit(
        self, *, runtime: SharedWorkspaceRuntime, cwd: Path,
        guest_uid: int | None, guest_gid: int | None,
    ) -> Path:
        """Return the real tool directory; fail before any Bubblewrap dispatch."""
        # Avoid a module-level circular import (workspace_supervision imports
        # the shared runtime to check its image pin).
        from potbot.tools.workspace_supervision import (
            WorkspaceLeaseGuard,
            validate_runtime_pin,
        )

        if not isinstance(self.slug, str) or not _TO.fullmatch(self.slug):
            raise InvalidInputError("maintenance tool must have an exact canonical slug")
        if not isinstance(self.agent, AgentHome) or not isinstance(
            self.guard, WorkspaceLeaseGuard
        ):
            raise ScopeDeniedError("writable tool mounts require a host-issued agent and lease")
        if not isinstance(self.agent.directory, str) or not _AGENT.fullmatch(
            self.agent.directory
        ):
            raise ScopeDeniedError("maintenance agent home is not a canonical directory")
        if guest_uid != self.agent.uid or guest_gid != self.agent.gid or guest_gid != 69:
            raise ScopeDeniedError("writable mount requires the admitted agent UID and group 69")
        request = self.guard.request
        if (
            request.operation not in {"tool.install", "tool.upgrade"}
            or request.effect != "write"
            or request.resources != (AccessResource("shared_tool", self.slug, "write"),)
            or request.workspace_id != self.agent.workspace_id
            or request.actor_id != self.agent.agent_id
            or request.image_digest != runtime.image_digest
            or self.guard.lease is None
            or self.guard.lease.workspace_id != self.agent.workspace_id
            or self.guard.lease.state != "active"
            or self.guard.fenced
        ):
            raise ScopeDeniedError("tool write grant has no matching active maintenance lease")
        validate_runtime_pin(self.guard.ledger, runtime, self.agent.workspace_id)
        root = runtime.home.resolve(strict=True)
        agent_home = root / self.agent.directory
        if agent_home.is_symlink() or not agent_home.is_dir():
            raise ScopeDeniedError("registered maintenance agent home was replaced")
        try:
            cwd.resolve(strict=True).relative_to(agent_home)
        except (OSError, ValueError) as exc:
            raise ScopeDeniedError("installer must run inside the admitted agent home") from exc
        alias = agent_home / "Tools"
        if not alias.is_symlink() or os.readlink(alias) != "/usr/local/tools":
            raise ScopeDeniedError("agent Tools alias differs from the guest tool mount")
        if runtime.shared_tools is None or runtime.shared_tools.is_symlink():
            raise ScopeDeniedError("workspace tool store is not a verified physical directory")
        tools = runtime.shared_tools.resolve(strict=True)
        target = tools / self.slug
        if target.is_symlink() or not target.is_dir() or target.resolve(strict=True).parent != tools:
            raise StateMismatchError("tool installer destination must be provisioned by the host")
        metadata = target.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or not metadata.st_mode & stat.S_IWGRP:
            raise ScopeDeniedError("tool installation is not shared-group writable")
        # Renew before admitting the write mount. The host runner must cancel
        # the process if this guard becomes fenced, and must not release until
        # the entire process tree has stopped.
        self.guard.ensure_live()
        return target
