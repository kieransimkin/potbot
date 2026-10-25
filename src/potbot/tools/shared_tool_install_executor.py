"""Controller-owned, lease-fenced execution of an admitted tool install command.

This is the execution leg of the shared tool installation workflow, NOT a
model-callable arbitrary shell. An operator configures exact argv fingerprints
and supplies independently verified postflight evidence. The next stage is
SharedToolStore.register; successful subprocess exit does not install a Cog or
grant execution authority.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import content_hash
from potbot.tools.ai_config_history import AiConfigHistoryMonitor
from potbot.tools.agent_homes import AgentHomeRegistry
from potbot.tools.shared_tool_mounts import ToolWriteGrant
from potbot.tools.shared_workspace_isolation import (
    SharedWorkspaceBubblewrapBackend,
    SharedWorkspaceRuntime,
)
from potbot.tools.supervisor import ProcessRequest, ProcessResult, ProcessSupervisor
from potbot.tools.workspace_concurrency import (
    AccessRequest,
    AccessResource,
    WorkspaceLeaseLedger,
)
from potbot.tools.workspace_supervision import (
    WorkspaceLeaseGuard,
    admission_digest,
    validate_runtime_pin,
)

_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


@dataclass(frozen=True, slots=True)
class ToolInstallReceipt:
    workspace_id: UUID
    agent_id: UUID
    slug: str
    command_digest: str
    image_digest: str
    isolation_identity: str
    observed_evidence_sha256: str
    output_sha256: str
    registered: bool = False  # Registration requires independent SharedToolStore action.


class SharedToolInstallExecutor:
    """Run an operator-pinned install/upgrade in one scoped writable tool mount.

    Operator-controlled ``allowed_commands`` maps tool slugs to the digest of
    an exact argv tuple. The argv is not accepted from an agent's model output;
    the controller must pin commands itself. The postflight callback must
    validate actual filesystem writes, cleanup and installed bytes and return
    a SHA-256 receipt. It runs only after confirmed process-tree termination.
    """

    def __init__(
        self,
        *,
        runtime: SharedWorkspaceRuntime,
        ledger: WorkspaceLeaseLedger,
        homes: AgentHomeRegistry,
        authorize: Callable[[UUID, UUID], bool],
        allowed_commands: Mapping[str, str],
        ai_monitor: AiConfigHistoryMonitor | None = None,
        supervisor_factory: Callable[[SharedWorkspaceBubblewrapBackend], ProcessSupervisor]
        | None = None,
    ) -> None:
        if not callable(authorize):
            raise ScopeDeniedError("tool installs require an operator authorization callback")
        if any(not _DIGEST.fullmatch(value) for value in allowed_commands.values()):
            raise InvalidInputError("operator-pinned install command digests must be SHA-256")
        self.runtime, self.ledger, self.homes = runtime, ledger, homes
        self.authorize, self.allowed_commands = authorize, dict(allowed_commands)
        self.ai_monitor = ai_monitor
        self.supervisor_factory = supervisor_factory or ProcessSupervisor

    def execute(
        self,
        *,
        workspace_id: UUID,
        agent_id: UUID,
        slug: str,
        argv: tuple[str, ...],
        idempotency_key: str,
        postflight: Callable[[ProcessResult], str],
        upgrade: bool = False,
    ) -> ToolInstallReceipt:
        if self.authorize(workspace_id, agent_id) is not True:
            raise ScopeDeniedError("agent is not approved to install workspace tools")
        if not isinstance(argv, tuple) or not argv or any(
            not isinstance(item, str) or not item or "\x00" in item for item in argv
        ):
            raise InvalidInputError("tool installation requires a pinned argv tuple")
        exact_digest = content_hash(list(argv))
        if self.allowed_commands.get(slug) != exact_digest:
            raise ScopeDeniedError("tool installation command differs from the operator pin")
        if not callable(postflight):
            raise ScopeDeniedError("tool installation requires independent postflight verification")
        if not argv[0].startswith("/") or argv[0].startswith("/usr/local/tools/"):
            raise ScopeDeniedError("installers must launch a verified rootfs executable")
        validate_runtime_pin(self.ledger, self.runtime, workspace_id)
        home = self.homes.lookup(workspace_id, agent_id)
        if home is None:
            raise ScopeDeniedError("installer agent has no registered home")
        agent_home = self.runtime.home / home.directory
        if agent_home.is_symlink() or not agent_home.is_dir():
            raise StateMismatchError("installer agent home is unavailable")
        operation = "tool.upgrade" if upgrade else "tool.install"
        # AccessResource validates slug before starting the lease. Every
        # installation is excluded from readers/writers of the *same* tool.
        resource = AccessResource("shared_tool", slug, "write")
        guard = WorkspaceLeaseGuard(
            self.ledger,
            AccessRequest(
                workspace_id, agent_id, operation, idempotency_key, "write",
                (resource,), self.runtime.image_digest,
            ),
        )
        guard.start()
        uncertain = True
        try:
            grant = ToolWriteGrant(slug, home, guard)
            # Force admission checks before constructing the supervised child.
            grant.admit(
                runtime=self.runtime, cwd=agent_home,
                guest_uid=home.uid, guest_gid=home.gid,
            )
            backend = SharedWorkspaceBubblewrapBackend(
                self.runtime, tool_write_grant=grant, ai_monitor=self.ai_monitor
            )
            supervisor = self.supervisor_factory(backend)
            if not isinstance(supervisor, ProcessSupervisor):
                raise ScopeDeniedError("installer must use Potbot ProcessSupervisor")
            request = ProcessRequest(
                argv=argv, cwd=agent_home,
                workspace_root=self.runtime.home, sandbox_root=self.runtime.home,
                guest_uid=home.uid, guest_gid=home.gid,
                telemetry_workspace_id=workspace_id, telemetry_agent_id=agent_id,
                telemetry_identity_kind="agent",
                timeout_ms=600_000, max_output_bytes=1_048_576,
                memory_bytes=8 * 1024**3, cpu_seconds=600,
            )
            result = supervisor.run(
                request, cancelled=lambda: guard.fenced or (
                    self.ai_monitor is not None and not self.ai_monitor.is_healthy()
                ),
            )
            if not result.processes_stopped:
                raise StateMismatchError("installer process termination is not confirmed")
            if result.outcome != "completed" or result.exit_code != 0 or not result.isolation_identity:
                raise StateMismatchError("install command failed; inspect and reconcile its write lease")
            evidence_sha = postflight(result)
            if not isinstance(evidence_sha, str) or not _DIGEST.fullmatch(evidence_sha):
                raise StateMismatchError("postflight must return verified SHA-256 evidence")
            guard.ensure_live()
            output_sha = hashlib.sha256(result.stdout + b"\0" + result.stderr).hexdigest()
            receipt = ToolInstallReceipt(
                workspace_id, agent_id, slug, exact_digest,
                self.runtime.image_digest, result.isolation_identity, evidence_sha,
                output_sha,
            )
            guard.finish(evidence_sha256=admission_digest(
                "installed:" + evidence_sha + ":" + exact_digest
            ))
            uncertain = False
            return receipt
        except BaseException:
            if uncertain:
                # Even nonzero exit can leave modified tool assets. The host
                # must reconcile; never retry a side-effecting install blindly.
                try:
                    guard.finish(evidence_sha256=admission_digest("tool-install-uncertain"),
                                 uncertain=True)
                except StateMismatchError:
                    guard.abandon()
            raise
