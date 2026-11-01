"""Normal host launch path for the dedicated context-capable worker profile.

Use after task/tool/budget approval, not as an HTTP launch endpoint. Existing
MCP/chat stdout protocols are never silently replaced with this RPC protocol.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from potbot.context.sessions import ContextSessionStore
from potbot.context.worker_channel import ContextWorkerChannel, _settle, validate_worker_binding
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError, StateMismatchError

if TYPE_CHECKING:
    from potbot.context.worker_channel import ContextWorkerResult
    from potbot.tools.agent_homes import AgentHomeRegistry
    from potbot.tools.ai_workspace_operator import AiWorkspaceOperator
    from potbot.tools.supervisor import ProcessRequest
    from potbot.tools.workspace_supervision import WorkspaceLeaseGuard


def _prepare(operator: AiWorkspaceOperator, registry: AgentHomeRegistry,
             principal: WorkspaceContext, request: ProcessRequest, guard: WorkspaceLeaseGuard,
             approve: Callable[[ProcessRequest], None]) -> Any:
    """Provision before admission; hold the operator lock through process claim."""
    from uuid import UUID

    from potbot.tools.interactive_supervisor import InteractiveProcessSupervisor

    with operator._lifecycle:
        operator._require_live()
        manager = operator._context_manager
        agent_id = request.telemetry_agent_id
        if (not isinstance(principal, WorkspaceContext) or not isinstance(agent_id, UUID)
                or not isinstance(request.telemetry_run_id, UUID)
                or request.telemetry_identity_kind != "agent"
                or request.telemetry_workspace_id != principal.workspace_id
                or operator.workspace_id != principal.workspace_id
                or manager is None or manager.principal != principal
                or registry.ledger is not operator.ledger or guard.ledger is not operator.ledger
                or guard.lease is not None or guard.request.actor_id != agent_id
                or guard.request.workspace_id != principal.workspace_id
                or guard.request.image_digest != operator.runtime.image_digest
                or not operator._project_agent_context
                or operator.supervisor._closing.is_set()):
            raise ScopeDeniedError("Context worker requires the managed operator and a fresh admitted lease")
        if not callable(approve) or approve(request) is not None:
            raise ScopeDeniedError("Worker approval must fail by raising, not by returning a flag")
        home = registry.lookup(principal.workspace_id, agent_id)
        if home is None:
            raise ScopeDeniedError("Context worker must already be registered")
        validate_worker_binding(request, home, Path(operator.ledger.home), principal, agent_id)
        # Existing projections were verified at operator start/refresh. A new
        # registered agent is reconciled before a worker can acquire its lease.
        bindings = operator.runtime.agent_context_projections
        if not any(item.agent_id == agent_id and item.directory == home.directory for item in bindings):
            from potbot.context.agent import AgentContextLifecycle, ContextIdentity

            AgentContextLifecycle(manager.service, registry).reconcile_existing(
                ContextIdentity(principal, agent_id))
            operator.refresh_context()
        if not any(item.agent_id == agent_id for item in operator.runtime.agent_context_projections):
            raise StateMismatchError("Agent context projection was not published")
        guard.start()
        return InteractiveProcessSupervisor(operator.backend)


async def dispatch_context_worker(
    operator: AiWorkspaceOperator, registry: AgentHomeRegistry, principal: WorkspaceContext,
    request: ProcessRequest, guard: WorkspaceLeaseGuard, *,
    approve: Callable[[ProcessRequest], None],
) -> ContextWorkerResult:
    """Provision, launch, bind RPC, revoke, stop, then release the process claim.

    `approve` is the host's existing task/budget/capability checker; neither it
    nor the request/guard/registry is deserialized from guest JSON. The caller
    retains responsibility for reconciling an abandoned process claim.
    """
    process = None
    launch = None
    try:
        supervisor = await _settle(lambda: _prepare(
            operator, registry, principal, request, guard, approve))
        launch = asyncio.create_task(supervisor.open(
            request, cancelled=lambda: guard.fenced or not operator.monitor.is_healthy()))
        process = await asyncio.shield(launch)
        manager = operator._context_manager
        agent_id = request.telemetry_agent_id
        if manager is None or agent_id is None:
            raise ScopeDeniedError("Context worker binding disappeared during launch")
        channel = ContextWorkerChannel(
            store=ContextSessionStore(manager.service.database),
            principal=principal, agent_id=agent_id, guard=guard,
            registry=registry, check_operator=operator.monitor.require_active,
        )
        result = await channel.serve(process)
        # A coherent receipt is not a claim that the guest command exited zero.
        # Callers inspect result.exit.return_code in their existing job outcome.
        evidence = hashlib.sha256(
            f"context-worker:{result.session_id}:{result.exit.isolation_identity}:"
            f"{result.channel.requests}:{result.channel.errors}:{result.exit.return_code}".encode()
        ).hexdigest()
        await _settle(lambda: guard.finish(evidence_sha256=evidence))
        return result
    except BaseException:
        async def retire() -> None:
            nonlocal process
            if launch is not None:
                try:
                    process = await launch
                except BaseException:
                    pass
            if process is not None:
                try:
                    await process.close(force=True)
                finally:
                    if guard.lease is not None:
                        guard.abandon()
            elif guard.lease is not None:
                guard.abandon()
        task = asyncio.create_task(retire())
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Retrieve cleanup error without replacing the original failure.
        if task.done() and not task.cancelled():
            task.exception()
        raise
