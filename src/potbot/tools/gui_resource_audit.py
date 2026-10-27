"""S15: host-owned kernel accounting for Xpra and controlled-browser processes.

Reuse WorkerMonitorSession's cgroup-v2, strace, TASKSTATS and file-index format;
never duplicate a weaker /proc polling implementation or place raw paths in UI.
The audited identity is the operator *principal*, not an invented agent UID.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from uuid import UUID

from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError
from potbot.tools.worker_telemetry import WorkerMonitorConfig, WorkerMonitorSession

if TYPE_CHECKING:
    from potbot.tools.shared_workspace_isolation import SharedWorkspaceRuntime

_SHA = re.compile(r"[0-9a-f]{64}\Z")


class GuiAuditHandle:
    """One checked process group; its outcome is never inferred from parent exit.

    command() registers TASKSTATS before the first child can spawn. started()
    begins cgroup sampling immediately after launch. finish() audits every traced
    descendant and returns False on *any* missing kernel coverage, trace overflow,
    lost monitor, or unconfirmed process-tree shutdown. No audit raw data is
    returned through this interface.
    """

    def __init__(self, session: WorkerMonitorSession) -> None:
        self._session = session
        self._lock = threading.RLock()
        self._prepared = False
        self._started = False
        self._finished: bool | None = None

    def command(self, argv: tuple[str, ...]) -> tuple[str, ...]:
        if (not isinstance(argv, tuple) or not argv or not all(
            isinstance(value, str) and value and "\x00" not in value for value in argv
        )):
            raise InvalidInputError("GUI monitor needs a bounded pinned child command")
        with self._lock:
            if self._prepared or self._finished is not None:
                raise InvalidInputError("GUI monitor command already prepared")
            self._prepared = True
            return self._session.command(argv)

    def started(self, pid: int) -> None:
        with self._lock:
            if (not self._prepared or self._started or self._finished is not None
                    or type(pid) is not int or not 0 < pid < 2**31):
                raise InvalidInputError("GUI monitor start must match one spawned process")
            self._started = True
            self._session.start(pid)

    @property
    def healthy(self) -> bool:
        with self._lock:
            return bool(self._started and self._finished is None and self._session.healthy)

    def finish(self, *, confirmed_stopped: bool) -> bool:
        """Finalize once; an unconfirmed parent can NEVER earn a clean receipt."""
        with self._lock:
            if self._finished is not None:
                return self._finished
            if not self._prepared:
                self._finished = False
                return False
            try:
                complete = bool(self._session.finish(confirmed_stopped=confirmed_stopped))
            except BaseException:
                # The operator retains the private fault/cgroup for reconciliation.
                complete = False
            self._finished = bool(self._started and confirmed_stopped and complete)
            return self._finished


class GuiAuditFactory:
    """An operator-pinned audit factory shared by Xpra and child application runs.

    Monitoring infrastructure is *never* exposed through /home, /etc/ai,
    /etc/ai-past, a model cache, the rootfs or the shared tools mount.
    """

    def __init__(
        self, *, config: WorkerMonitorConfig, runtime: SharedWorkspaceRuntime,
        workspace_id: UUID, principal_id: UUID,
        session_factory: Callable[..., WorkerMonitorSession] = WorkerMonitorSession,
    ) -> None:
        if not isinstance(workspace_id, UUID) or not isinstance(principal_id, UUID):
            raise InvalidInputError("GUI telemetry requires an authenticated principal")
        if not isinstance(config, WorkerMonitorConfig):
            raise CapabilityUnavailableError("GUI telemetry requires qualified kernel collection")
        if not callable(session_factory):
            raise InvalidInputError("GUI monitor factory must be host-supplied")
        guest_roots = (runtime.rootfs, runtime.home, runtime.model_staging,
                       runtime.shared_models, runtime.shared_tools,
                       runtime.ai_config_dir, runtime.ai_history_dir)
        selected = [Path(root).resolve(strict=True) for root in guest_roots if root is not None]
        assets = (config.audit_root, config.cgroup_root, config.strace, config.cgroup_join)
        for asset in assets:
            path = Path(asset).resolve(strict=True)
            if any(path == root or path.is_relative_to(root) for root in selected):
                raise ScopeDeniedError("GUI audit infrastructure overlaps a guest mount")
        self.config = config
        self.workspace_id = workspace_id
        self.principal_id = principal_id
        self.session_factory = session_factory

    @classmethod
    def from_environment(
        cls, *, runtime: SharedWorkspaceRuntime, workspace_id: UUID, principal_id: UUID,
    ) -> GuiAuditFactory:
        # Host-only environment chosen by the service manager, not guest input.
        return cls(config=WorkerMonitorConfig.from_environment(), runtime=runtime,
                   workspace_id=workspace_id, principal_id=principal_id)

    def __call__(
        self, isolation_identity: str, run_id: UUID, *,
        agent_id: UUID | None = None, agent_registry: object | None = None,
    ) -> GuiAuditHandle:
        if not isinstance(isolation_identity, str) or not _SHA.fullmatch(isolation_identity):
            raise InvalidInputError("GUI audit needs a pinned isolation hash")
        if not isinstance(run_id, UUID):
            raise InvalidInputError("GUI audit needs an immutable run identifier")
        subject, kind = self.principal_id, "principal"
        if agent_id is not None:
            # Only the controller-owned S40 registry may establish that a
            # browser really belongs to a specific enrolled agent.
            from potbot.tools.agent_homes import AgentHomeRegistry
            if not isinstance(agent_id, UUID) or not isinstance(agent_registry, AgentHomeRegistry):
                raise ScopeDeniedError("GUI attribution requires an admitted agent registry")
            identity = agent_registry.lookup(self.workspace_id, agent_id)
            if identity is None or identity.workspace_id != self.workspace_id or identity.agent_id != agent_id:
                raise ScopeDeniedError("GUI agent identity is not registered")
            subject, kind = agent_id, "agent"
        elif agent_registry is not None:
            raise ScopeDeniedError("GUI attribution must not accept an unused agent registry")
        session = self.session_factory(
            self.config, self.workspace_id, subject, isolation_identity,
            identity_kind=kind, run_id=run_id,
        )
        return GuiAuditHandle(session)
