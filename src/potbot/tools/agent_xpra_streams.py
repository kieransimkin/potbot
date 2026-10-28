"""S41: operator-only, per-agent Xpra streaming tickets.

This authority never creates an X server or grants an agent a new browser.
It observes a previously admitted agent browser, binds a one-use ticket to the
actual private AF_UNIX protocol socket inode, and revalidates both at redemption.
No Xauthority, PID, path, agent reservation token or host socket is returned to
HTTP clients. Invalid/expired tickets must not be retried automatically.
"""
from __future__ import annotations

import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError, StateMismatchError

_ID = re.compile(r"[0-9a-f]{32}\Z")
_TICKET = re.compile(r"[A-Za-z0-9_-]{43}\Z")


@dataclass(frozen=True, slots=True)
class AgentStreamBinding:
    agent_id: UUID
    session_id: str
    socket: Path
    device: int
    inode: int
    created_ns: int


@dataclass(frozen=True, slots=True)
class _Pending:
    expires_at: float
    agent_id: UUID
    session_id: str
    device: int
    inode: int
    created_ns: int


class AgentXpraTicketAuthority:
    """Bounded one-use admission for human read-only views, not a control API."""

    def __init__(self, server: Any, apps: Any, *, clock: Callable[[], float] = time.monotonic,
                 lifetime: int = 30, maximum: int = 64) -> None:
        if not hasattr(server, "workspace_id") or not isinstance(server.workspace_id, UUID):
            raise InvalidInputError("agent stream authority requires an owning workspace")
        if not isinstance(apps.workspace_id, UUID) or apps.workspace_id != server.workspace_id:
            raise ScopeDeniedError("agent stream supervisor differs from Xpra workspace")
        if getattr(apps, "agent_displays", None) is None or server.readonly_mode is not True:
            raise CapabilityUnavailableError("agent streams require private Xpra and readonly operator mode")
        if not callable(clock) or not 1 <= lifetime <= 60 or not 1 <= maximum <= 128:
            raise InvalidInputError("agent Xpra ticket bounds invalid")
        self.server, self.apps, self.clock = server, apps, clock
        self.lifetime, self.maximum = lifetime, maximum
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}

    def inventory(self) -> dict[str, object]:
        # Independently admitted service. This intentionally does not give
        # browser commands, agent reservations or agent-launch capability.
        self.server.environment()
        return {"workspaceId": str(self.server.workspace_id),
                "sessions": self.apps.agent_display_inventory(), "viewOnly": True}

    def _binding(self, agent_id: UUID, session_id: str) -> AgentStreamBinding:
        if not isinstance(agent_id, UUID) or not isinstance(session_id, str) or not _ID.fullmatch(session_id):
            raise InvalidInputError("invalid agent display identity")
        self.server.environment()
        socket = self.apps.agent_stream_socket(agent_id, session_id)
        if not isinstance(socket, Path) or socket.is_symlink():
            raise ScopeDeniedError("private Xpra protocol socket identity changed")
        info = socket.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self._uid():
            raise ScopeDeniedError("private Xpra protocol socket is not operator-owned")
        return AgentStreamBinding(agent_id, session_id, socket, info.st_dev, info.st_ino, info.st_ctime_ns)

    @staticmethod
    def _uid() -> int:
        import os
        return os.getuid()

    def issue(self, agent_id: UUID, session_id: str) -> dict[str, object]:
        candidate = self._binding(agent_id, session_id)
        with self._lock:
            now = self.clock()
            self._pending = {k: v for k, v in self._pending.items() if v.expires_at > now}
            if len(self._pending) >= self.maximum:
                raise CapabilityUnavailableError("agent display ticket capacity reached")
            ticket = secrets.token_urlsafe(32)
            self._pending[ticket] = _Pending(
                now + self.lifetime, agent_id, session_id, candidate.device, candidate.inode, candidate.created_ns,
            )
        return {"workspaceId": str(self.server.workspace_id), "ticket": ticket,
                "expiresInSeconds": self.lifetime, "transport": "xpra-native-binary",
                "viewOnly": True}

    def consume(self, ticket: str) -> AgentStreamBinding:
        if not isinstance(ticket, str) or not _TICKET.fullmatch(ticket):
            raise ScopeDeniedError("agent Xpra ticket is invalid")
        # Consume before checking anything else; failure never restores ticket.
        with self._lock:
            value = self._pending.pop(ticket, None)
        if value is None or self.clock() >= value.expires_at:
            raise ScopeDeniedError("agent Xpra ticket expired or already used")
        current = self._binding(value.agent_id, value.session_id)
        if (current.device, current.inode, current.created_ns) != (value.device, value.inode, value.created_ns):
            raise ScopeDeniedError("private Xpra socket changed after ticket issuance")
        return current

    def still_live(self, binding: AgentStreamBinding) -> bool:
        try:
            now = self._binding(binding.agent_id, binding.session_id)
            return (now.device, now.inode, now.created_ns) == (binding.device, binding.inode, binding.created_ns)
        except (OSError, ValueError, RuntimeError, CapabilityUnavailableError, ScopeDeniedError, StateMismatchError):
            return False
