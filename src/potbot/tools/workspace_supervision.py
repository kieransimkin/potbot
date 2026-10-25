"""Host-only lease lifetime around an admitted shared-home executable.

A lease coordinates cooperative tool gateways. It does NOT stop an unbrokered
process writing to /home. A pinned, qualified Bubblewrap backend is a separate
mandatory prerequisite for hostile commands.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.tools.shared_workspace_isolation import SharedWorkspaceRuntime
from potbot.tools.workspace_concurrency import (
    AccessLease,
    AccessRequest,
    AccessResource,
    WorkspaceLeaseLedger,
)


@dataclass(frozen=True, slots=True)
class WorkspaceExecutorIdentity:
    """Identity granted by the trusted host, never supplied by model output."""

    workspace_id: UUID
    actor_id: UUID
    image_digest: str


def validate_runtime_pin(
    ledger: WorkspaceLeaseLedger,
    runtime: SharedWorkspaceRuntime,
    workspace_id: UUID,
) -> None:
    """Require image, home and qualification to match the host's active pin."""
    pin = ledger.image_status(workspace_id)
    if pin is None or (
        pin["digest"] != runtime.image_digest
        or pin["qualification_sha256"] != runtime.qualification_sha256
        or pin["rootfs_path"] != str(runtime.rootfs.resolve(strict=True))
        or ledger.home != runtime.home.resolve(strict=True)
    ):
        raise StateMismatchError("workspace runtime differs from the pinned qualified image")


class WorkspaceLeaseGuard:
    """Periodically renew a full-/home exclusive lease during an unsafe process.

    When renewal fails, the caller's cancellation predicate MUST terminate the
    process. Closing an unconfirmed process does not release its write conflict.
    """

    def __init__(
        self,
        ledger: WorkspaceLeaseLedger,
        request: AccessRequest,
        *,
        ttl_seconds: int = 90,
        interval_seconds: float = 10.0,
    ) -> None:
        if not 1 <= ttl_seconds <= 300 or not 0.05 <= interval_seconds < ttl_seconds:
            raise ValueError("invalid lease renewal schedule")
        self.ledger = ledger
        self.request = request
        self.ttl_seconds = ttl_seconds
        self.interval_seconds = interval_seconds
        self.lease: AccessLease | None = None
        self._stopped = threading.Event()
        self._fenced = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._finished = False

    @classmethod
    def exclusive_home(
        cls,
        ledger: WorkspaceLeaseLedger,
        identity: WorkspaceExecutorIdentity,
        *,
        operation: str,
        idempotency_key: str,
        ttl_seconds: int = 90,
        interval_seconds: float = 10.0,
    ) -> WorkspaceLeaseGuard:
        request = AccessRequest(
            identity.workspace_id,
            identity.actor_id,
            operation,
            idempotency_key,
            "unknown",
            (AccessResource("workspace", "*", "write"),),
            identity.image_digest,
        )
        return cls(ledger, request, ttl_seconds=ttl_seconds, interval_seconds=interval_seconds)

    @property
    def fenced(self) -> bool:
        return self._fenced.is_set()

    def start(self) -> AccessLease:
        with self._lock:
            if self.lease is not None:
                raise StateMismatchError("lease guard has already started")
            lease = self.ledger.claim(
                self.request, ttl_seconds=self.ttl_seconds, replay_existing=False
            )
            if lease.state != "active":
                raise StateMismatchError("previous tool attempt requires reconciliation")
            self.lease = lease
            self._thread = threading.Thread(target=self._renew, daemon=True, name="potbot-home-lease")
            self._thread.start()
            return lease

    def _renew(self) -> None:
        while not self._stopped.wait(self.interval_seconds):
            try:
                with self._lock:
                    if self._finished or self.lease is None:
                        return
                    self.lease = self.ledger.renew(self.lease, ttl_seconds=self.ttl_seconds)
            except BaseException:
                self._fenced.set()
                return

    def ensure_live(self) -> None:
        with self._lock:
            if self._fenced.is_set() or self._finished or self.lease is None:
                raise StateMismatchError("workspace lease has been fenced")
            try:
                self.lease = self.ledger.renew(self.lease, ttl_seconds=self.ttl_seconds)
            except BaseException as exc:
                self._fenced.set()
                raise StateMismatchError("workspace lease renewal failed") from exc

    def finish(self, *, evidence_sha256: str, uncertain: bool = False) -> None:
        """Release only after process shutdown and verified host-side observations.

        An expired/fenced lease stays indeterminate for host reconciliation.
        Never manufacture a successful receipt from an unknown process outcome.
        """
        with self._lock:
            if self._finished:
                raise StateMismatchError("workspace lease already finalized")
            if self.lease is None:
                raise StateMismatchError("workspace lease was not started")
            self._stopped.set()
            if self._fenced.is_set() and not uncertain:
                raise StateMismatchError("cannot complete a fenced workspace lease")
            self.ledger.finish(
                self.lease,
                result_sha256=evidence_sha256,
                uncertain=uncertain or self._fenced.is_set(),
            )
            self._finished = True

    def abandon(self) -> None:
        """Stop renewal; expiry will conservatively fence a possibly running effect."""
        self._stopped.set()


def admission_digest(reason: str) -> str:
    return hashlib.sha256(reason.encode("utf-8")).hexdigest()


def assert_shared_execution_allowed(
    *, runtime: SharedWorkspaceRuntime, ledger: WorkspaceLeaseLedger,
    workspace_id: UUID, candidate_path: str, authorization: Callable[[UUID], bool] | None = None,
) -> None:
    if authorization is not None and authorization(workspace_id) is not True:
        raise ScopeDeniedError("workspace shell/tool execution not authorized")
    validate_runtime_pin(ledger, runtime, workspace_id)
    from pathlib import Path

    candidate = Path(candidate_path).resolve(strict=True)
    home = runtime.home.resolve(strict=True)
    if candidate != home and not candidate.is_relative_to(home):
        raise ScopeDeniedError("tool candidate must reside inside shared workspace /home")
