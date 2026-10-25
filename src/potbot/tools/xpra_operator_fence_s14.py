"""S14: fail-closed owner-epoch fence for Xpra and controlled-browser processes.

The operator health channel is a *host-only* Unix socket. This guard neither
starts a second /etc/ai watcher nor exports operator capabilities to browsers.
An epoch change or lost owner permanently fences this Xpra service instance.
All admission and browser-frame relays must call require_healthy; an independent
watchdog tears down existing applications and the owned display on owner loss.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError
from potbot.tools.operator_attachment_s13 import OperatorAttachmentClient

if TYPE_CHECKING:
    from potbot.tools.x11_workspace_server import WorkspaceXServer


class XpraOperatorFence:
    """Attach an Xpra service to exactly one live, pinned workspace operator.

    A fenced instance MUST be restarted by the native service; it must never
    attach to a replacement epoch. Application lease reconciliation remains
    the responsibility of XpraApplicationSupervisor and the host ledger.
    """

    def __init__(
        self,
        *,
        attachment: OperatorAttachmentClient,
        display: WorkspaceXServer,
        applications: Any = None,
        check_interval_seconds: float = 0.2,
    ) -> None:
        if not isinstance(attachment, OperatorAttachmentClient):
            raise ScopeDeniedError("Xpra operator fencing requires a host-issued client")
        if (
            not isinstance(display.workspace_id, UUID)
            or attachment.workspace_id != display.workspace_id
        ):
            raise ScopeDeniedError("Xpra display is outside the attached operator workspace")
        if not 0.05 <= check_interval_seconds <= 2.0:
            raise InvalidInputError("Xpra monitoring interval is unsupported")
        self.attachment = attachment
        self.display = display
        self.applications = applications
        self.interval = check_interval_seconds
        self._closed = threading.Event()
        self._fenced = threading.Event()
        self._started = False
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None

    @property
    def fenced(self) -> bool:
        return self._fenced.is_set()

    def require_healthy(self) -> dict[str, object]:
        if self._closed.is_set() or self._fenced.is_set():
            raise CapabilityUnavailableError("Xpra operator attachment is fenced")
        try:
            status = self.attachment.require_healthy()
        except Exception:
            self._fence()
            raise CapabilityUnavailableError("Xpra operator attachment is unavailable") from None
        if self._fenced.is_set() or self._closed.is_set():
            raise CapabilityUnavailableError("Xpra operator attachment is fenced")
        return status

    def healthy(self) -> bool:
        try:
            self.require_healthy()
            return True
        except CapabilityUnavailableError:
            return False

    def start(self) -> XpraOperatorFence:
        with self._lock:
            if self._started or self._closed.is_set() or self._fenced.is_set():
                raise CapabilityUnavailableError("Xpra owner fence cannot be restarted")
            self.require_healthy()
            self._started = True
            self._thread = threading.Thread(
                target=self._watch, daemon=True, name="potbot-xpra-owner-watch"
            )
            self._thread.start()
        return self

    def _watch(self) -> None:
        while not self._closed.wait(self.interval):
            if not self.healthy():
                return

    def _fence(self) -> None:
        # Set the latch before touching subprocesses, so concurrent HTTP/WS
        # admission immediately fails even if process-group reaping is slow.
        with self._lock:
            if self._fenced.is_set():
                return
            self._fenced.set()
            self._closed.set()
        if self.applications is not None:
            try:
                self.applications.fence_owner()
            except Exception:
                # Never claim the session or its lease was cleanly reaped.
                pass
        try:
            self.display.stop()
        except Exception:
            pass

    def close(self) -> None:
        self._closed.set()
        thread = self._thread
        if thread is not None and threading.current_thread() is not thread:
            thread.join(timeout=3.0)

    def __enter__(self) -> XpraOperatorFence:
        return self.start()

    def __exit__(self, *_args: object) -> None:
        self.close()
