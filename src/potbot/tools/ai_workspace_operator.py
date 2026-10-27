"""Operator-owned lifecycle for /etc/ai watching and supervised workspace work.

The owner must outlive every guest process. Its supervisor cancels on watcher
failure and fences new dispatch after shutdown begins. Shared tool installs and
PTYs must receive the same owner-held monitor; their own leases and process
termination remain mandatory. This is not a controller network endpoint.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.context.projection import ContextProjectionManager
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools.ai_config_history import AiConfigHistoryMonitor
from potbot.tools.shared_workspace_isolation import (
    SharedWorkspaceBubblewrapBackend,
    SharedWorkspaceRuntime,
)
from potbot.tools.supervisor import ProcessRequest, ProcessResult, ProcessSupervisor
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger
from potbot.tools.workspace_supervision import validate_runtime_pin

if TYPE_CHECKING:
    from potbot.tools.secrets import CaptureRedactor
    from potbot.tools.shared_tool_mounts import ToolWriteGrant


class AiManagedProcessSupervisor(ProcessSupervisor):
    """Wrap the existing qualified supervisor, tracking outstanding executions.

    ``quiesce`` stops new dispatch and asks existing runs to cancel; it never
    claims a stopped process without the underlying ProcessSupervisor receipt.
    """

    def __init__(self, backend: SharedWorkspaceBubblewrapBackend,
                 monitor: AiConfigHistoryMonitor) -> None:
        if backend.ai_monitor is not monitor:
            raise StateMismatchError("managed supervisor requires its exact operator watcher")
        super().__init__(backend)
        self._monitor = monitor
        self._gate = threading.Condition()
        self._closing = threading.Event()
        self._active = 0

    @property
    def active_runs(self) -> int:
        with self._gate:
            return self._active

    def run(
        self, request: ProcessRequest, *,
        cancelled: Callable[[], bool] | None = None,
        redactor: CaptureRedactor | None = None,
    ) -> ProcessResult:
        with self._gate:
            if self._closing.is_set():
                raise CapabilityUnavailableError("AI workspace operator is shutting down")
            self._monitor.require_active()
            self._active += 1
        try:
            def cancel_on_fence() -> bool:
                return (self._closing.is_set() or not self._monitor.is_healthy()
                        or (cancelled is not None and cancelled()))

            return super().run(request, cancelled=cancel_on_fence, redactor=redactor)
        finally:
            with self._gate:
                self._active -= 1
                self._gate.notify_all()

    def quiesce(self, *, timeout_seconds: float = 10.0) -> None:
        """Cancel outstanding executions and demand confirmed supervisor exit."""
        if not 0 < timeout_seconds <= 120:
            raise InvalidInputError("operator shutdown wait must be bounded")
        with self._gate:
            self._closing.set()
            if not self._gate.wait_for(lambda: self._active == 0, timeout_seconds):
                raise StateMismatchError("guest processes remain active; keep AI history running")


class AiWorkspaceOperator:
    """Single host-process owner of history monitor and qualified run backend.

    The host must provision sources *before* construction. The owner refuses
    stop while workspace leases remain active (including uncertain PTYs and
    tool installations). Call close only after all admitted guests have ended.
    """

    def __init__(
        self, *, workspace_id: UUID, ledger: WorkspaceLeaseLedger,
        runtime: SharedWorkspaceRuntime,
        monitor_interval_seconds: float = 0.15,
        context_manager: ContextProjectionManager | None = None,
    ) -> None:
        if not isinstance(workspace_id, UUID) or not isinstance(ledger, WorkspaceLeaseLedger):
            raise InvalidInputError("operator owner needs a registered workspace and lease ledger")
        if not isinstance(runtime, SharedWorkspaceRuntime):
            raise InvalidInputError("operator owner needs its pinned shared workspace runtime")
        if runtime.ai_config_dir is None or runtime.ai_history_dir is None:
            raise CapabilityUnavailableError("modern workspace requires both AI configuration mounts")
        self.workspace_id, self.ledger, self.runtime = workspace_id, ledger, runtime
        if context_manager is not None and not isinstance(context_manager, ContextProjectionManager):
            raise InvalidInputError("context manager must be a trusted operator instance")
        self._context_manager = context_manager
        self._interval = monitor_interval_seconds
        self._monitor: AiConfigHistoryMonitor | None = None
        self._backend: SharedWorkspaceBubblewrapBackend | None = None
        self._supervisor: AiManagedProcessSupervisor | None = None
        self._started = False
        self._closed = False
        self._lifecycle = threading.RLock()

    def _sources(self) -> tuple[Path, Path]:
        config, past = self.runtime.ai_config_dir, self.runtime.ai_history_dir
        if config is None or past is None:
            raise CapabilityUnavailableError("AI mount sources disappeared from the runtime")
        return Path(config), Path(past)

    def start(self) -> AiWorkspaceOperator:
        with self._lifecycle:
            if self._started or self._closed:
                raise StateMismatchError("AI workspace operator cannot be started twice")
            validate_runtime_pin(self.ledger, self.runtime, self.workspace_id)
            if self._context_manager is not None:
                if self._context_manager.principal.workspace_id != self.workspace_id:
                    raise ScopeDeniedError("operator context belongs to another workspace")
                if self.runtime.context_projection is not None:
                    raise InvalidInputError("operator context projection was already supplied")
                from potbot.context.model import ROLES

                for role in ROLES:
                    target = self.runtime.rootfs / role
                    if target.is_symlink() or not target.is_file() or target.stat().st_size != 0:
                        raise CapabilityUnavailableError("image has no safe root context placeholder")
                guest_mounts = (
                    self.runtime.rootfs, self.runtime.home, self.runtime.model_staging,
                    self.runtime.shared_models, self.runtime.shared_tools,
                    self.runtime.ai_config_dir, self.runtime.ai_history_dir,
                    self.runtime.shared_skills,
                )
                for mount in guest_mounts:
                    if mount is None:
                        continue
                    path = Path(mount).resolve(strict=True)
                    parent = self._context_manager.parent
                    if path == parent or path.is_relative_to(parent) or parent.is_relative_to(path):
                        raise ScopeDeniedError("operator context storage overlaps a guest mount")
                try:
                    projection = self._context_manager.capture()
                except BaseException:
                    self._context_manager.close()
                    raise
                self.runtime = replace(self.runtime, context_projection=projection)
            config, past = self._sources()
            monitor: AiConfigHistoryMonitor | None = None
            try:
                monitor = AiConfigHistoryMonitor(
                    config_dir=config, past_dir=past,
                    interval_seconds=self._interval,
                )
                monitor.start()  # Full persisted replay audit runs before any mount.
                backend = SharedWorkspaceBubblewrapBackend(self.runtime, ai_monitor=monitor)
                supervisor = AiManagedProcessSupervisor(backend, monitor)
            except BaseException:
                if monitor is not None:
                    monitor.stop()
                if self._context_manager is not None:
                    self._context_manager.close()
                raise
            self._monitor = monitor
            self._backend = backend
            self._supervisor = supervisor
            self._started = True
            return self

    def _require_live(self) -> None:
        if not self._started or self._closed or self._monitor is None:
            raise CapabilityUnavailableError("AI workspace operator is not running")
        config, past = self._sources()
        self._monitor.require_active(config_dir=config, past_dir=past)

    @property
    def monitor(self) -> AiConfigHistoryMonitor:
        self._require_live()
        assert self._monitor is not None
        return self._monitor

    @property
    def backend(self) -> SharedWorkspaceBubblewrapBackend:
        self._require_live()
        assert self._backend is not None
        return self._backend

    @property
    def supervisor(self) -> AiManagedProcessSupervisor:
        self._require_live()
        assert self._supervisor is not None
        return self._supervisor

    def installer_backend(self, grant: ToolWriteGrant) -> SharedWorkspaceBubblewrapBackend:
        """Produce an installer-specific backend, never change the base mount."""
        self._require_live()
        return SharedWorkspaceBubblewrapBackend(
            self.runtime, ai_monitor=self.monitor, tool_write_grant=grant,
        )

    def gateway_parameters(self) -> dict[str, Any]:
        """Pass these to ToolGateway alongside its ordinary controller services."""
        self._require_live()
        return {
            "shared_runtime": self.runtime,
            "shared_leases": self.ledger,
            "ai_monitor": self.monitor,
            "supervisor": self.supervisor,
        }

    def status(self) -> dict[str, object]:
        monitor = self._monitor
        worker = self._supervisor
        # Read the S12 host-only gate ledger. Unknown schema is reported as
        # unknown, never as a clean or released workspace.
        with self.ledger._connect() as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='trusted_tool_release_gates'"
            ).fetchone()
            pending: int | None = None
            if exists is not None:
                record = db.execute(
                    "SELECT COUNT(*) AS n FROM trusted_tool_release_gates "
                    "WHERE workspace_id=? AND state NOT IN ('released','rolled_back')",
                    (str(self.workspace_id),),
                ).fetchone()
                pending = int(record["n"])
        return {
            "schema": "potbot-ai-workspace-operator/v1",
            "workspace_id": str(self.workspace_id),
            "image_digest": self.runtime.image_digest,
            "running": self._started and not self._closed,
            "history_healthy": bool(monitor is not None and monitor.is_healthy()),
            "supervised_runs": worker.active_runs if worker is not None else 0,
            "pending_release_gates": pending,
        }

    def close(self, *, timeout_seconds: float = 10.0) -> None:
        """Cancel supervised work, require no live lease, flush, THEN stop."""
        with self._lifecycle:
            if self._closed:
                return
            if not self._started or self._supervisor is None or self._monitor is None:
                raise StateMismatchError("AI workspace operator has not been started")
            self._supervisor.quiesce(timeout_seconds=timeout_seconds)
            if self.ledger.active(self.workspace_id):
                raise StateMismatchError(
                    "workspace leases are still active/uncertain; monitoring must continue"
                )
            # Final settled-state reconciliation before relinquishing the
            # singleton journal. Even an already-faulted watcher must release
            # its operator lock AFTER all workers/leases have stopped; the
            # persisted evidence stays unreconciled and restart fails closed.
            try:
                self._monitor.poll_once()
            finally:
                self._monitor.stop()
                self._closed = True
                if self._context_manager is not None:
                    self._context_manager.close()

    def __enter__(self) -> AiWorkspaceOperator:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.close()
