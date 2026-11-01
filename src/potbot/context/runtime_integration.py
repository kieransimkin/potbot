"""Context for the existing admitted Cog runner, without changing legacy wire bytes.

The controller binds its restricted database once. Each launch gets independent
read-only projections. A result receipt is NOT process-stop evidence. Uncertain
launches retain their projections and ordinary process lease for reconciliation.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.context.agent import AgentContextLifecycle, ContextIdentity
from potbot.context.catalogue import RuntimeObject, register_runtime_objects
from potbot.context.projection import AgentContextProjection, ContextProjectionManager
from potbot.context.service import WorkspaceContextService
from potbot.context.sessions import ContextSessionStore
from potbot.context.skill_projection import _directory
from potbot.context.work_exchange import ContextWorkExchange, bounded_work
from potbot.context.worker_channel import ContextWorkerChannel, _settle
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    CapabilityUnavailableError, IndeterminateExecutionError, ScopeDeniedError, StateMismatchError,
)


def context_parent(ledger: Any, runtime: Any, workspace_id: UUID) -> Path:
    """An owned per-workspace directory outside every guest-visible mount."""
    parent = _directory(Path(ledger.path).parent)
    target = parent / ("context-" + workspace_id.hex)
    for area in (runtime.rootfs, runtime.home, runtime.model_staging, runtime.shared_models,
                 runtime.shared_tools, runtime.shared_skills, runtime.ai_config_dir,
                 runtime.ai_history_dir):
        if area is None:
            continue
        other = Path(area).resolve(strict=True)
        if other == target or other.is_relative_to(target) or target.is_relative_to(other):
            raise ScopeDeniedError("Context storage overlaps a guest mount")
    if not target.exists():
        target.mkdir(mode=0o700, exist_ok=True)
    _directory(target)
    if target.stat().st_mode & 0o077:
        raise ScopeDeniedError("Context projection storage must be private")
    return target


def operation_record(operation: Any) -> RuntimeObject:
    family = {"workflow": "workflow", "mcp_workflow": "workflow", "model": "model",
              "mcp_server": "connector", "mcp_session": "connector", "api": "connector"}
    material = {"binding": operation.binding.to_dict(), "argv": operation.argv,
                "executable_sha256": operation.executable_sha256,
                "protocol": operation.protocol}
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":"),
                                      allow_nan=False).encode()).hexdigest()
    return RuntimeObject(family.get(operation.binding.kind, "tool"),
                         str(operation.binding.operation_ref), digest)


@dataclass
class LaunchContext:
    manager: ContextProjectionManager
    backend: Any

    def close(self) -> None:
        self.manager.close()


class ContextExecutionBinding:
    """Installed by ExecutionCogRuntime, not a guest-selectable profile or DSN."""

    def __init__(self, database: Any, principal: WorkspaceContext) -> None:
        if not isinstance(principal, WorkspaceContext):
            raise ScopeDeniedError("Execution context requires authenticated identity")
        self.database, self.principal = database, principal
        self.service = WorkspaceContextService(database)

    def install(self, operations: tuple[Any, ...]) -> None:
        objects = [operation_record(op) for op in operations]
        objects.append(RuntimeObject("gateway", "potbot.execution-cogs/v1",
                                     hashlib.sha256(b"potbot.execution-cogs/v1").hexdigest()))
        with self.database.transaction(self.principal) as conn:
            register_runtime_objects(conn, self.principal, objects)

    def check(self, executor: Any, actor: WorkspaceContext) -> None:
        if actor != self.principal:
            raise ScopeDeniedError("Context executor belongs to another principal")
        closing = getattr(executor.supervisor, "_closing", None)
        if closing is not None and closing.is_set():
            raise ScopeDeniedError("Operator execution is stopping")
        backend = executor.supervisor.backend
        if backend.ai_monitor is not None:
            backend.ai_monitor.require_active()
        elif backend.operator_attachment is not None:
            backend.operator_attachment.require_healthy()
        else:
            raise CapabilityUnavailableError("Context execution requires a live operator")

    def prepare(self, executor: Any, actor: WorkspaceContext, agent_id: UUID,
                run_id: UUID) -> LaunchContext:
        from potbot.tools.shared_workspace_isolation import SharedWorkspaceBubblewrapBackend

        self.check(executor, actor)
        runtime = executor.runtime
        from potbot.context.object_projection import ObjectContextProjector
        from potbot.context.skill_projection import SkillLibraryProjector, prepare_skill_root

        candidate = runtime.shared_skills or (Path(executor.ledger.path).parent /
                                               ("skills-" + actor.workspace_id.hex))
        physical = candidate.resolve()
        for area in (runtime.rootfs, runtime.home, runtime.model_staging, runtime.shared_models,
                     runtime.shared_tools, runtime.ai_config_dir, runtime.ai_history_dir):
            if area is not None:
                other = Path(area).resolve(strict=True)
                if physical == other or physical.is_relative_to(other) or other.is_relative_to(physical):
                    raise ScopeDeniedError("Skill library overlaps another guest mount")
        skills = (runtime.shared_skills or prepare_skill_root(
            Path(executor.ledger.path).parent, actor.workspace_id))
        runtime = replace(runtime, shared_skills=skills)
        home = executor.homes.lookup(actor.workspace_id, agent_id)
        if home is None:
            raise ScopeDeniedError("Context executor requires a registered home")
        AgentContextLifecycle(self.service, executor.homes).reconcile_existing(
            ContextIdentity(actor, agent_id))
        self.service.provision_private(actor, "worker", agent_id.hex + "." + run_id.hex,
            idempotency_key="worker-run-" + agent_id.hex + "-" + run_id.hex)
        SkillLibraryProjector(skills).sync(self.service, actor)
        ObjectContextProjector(skills).sync(self.service, actor)
        parent = context_parent(executor.ledger, runtime, actor.workspace_id)
        manager = ContextProjectionManager(service=self.service, principal=actor, parent=parent)
        try:
            root = manager.capture()
            agent = manager.capture_object("agent", str(agent_id))
            binding = AgentContextProjection(agent_id, home.directory, agent)
            binding.inspect(home=runtime.home)
            selected = replace(runtime, context_projection=root, agent_context_projections=(binding,))
            original = executor.supervisor.backend
            backend = SharedWorkspaceBubblewrapBackend(selected, ai_monitor=original.ai_monitor,
                executable=original.executable, prlimit=original.prlimit)
            self.check(executor, actor)
            return LaunchContext(manager, backend)
        except BaseException:
            manager.close()
            raise


async def execute_context_operation(executor: Any, op: Any, source: Any, cog: Any,
                                    actor: WorkspaceContext, agent_id: UUID,
                                    cancel: threading.Event, syn_fault: threading.Event) -> Any:
    """Typed work and context RPC on one declared protocol; no stdout sniffing."""
    from potbot.cogs.contracts import TypedCogValue
    from potbot.tools.interactive_supervisor import InteractiveProcessSupervisor
    from potbot.tools.supervisor import ProcessRequest
    from potbot.tools.workspace_supervision import (
        WorkspaceExecutorIdentity, WorkspaceLeaseGuard, assert_shared_execution_allowed,
    )

    binding = executor._context_binding
    if binding is None:
        raise ScopeDeniedError("Context work requires the authenticated controller binding")
    binding.check(executor, actor)
    home = executor.homes.lookup(actor.workspace_id, agent_id)
    if home is None:
        raise ScopeDeniedError("Worker is not registered")
    cwd = executor.runtime.home / home.directory
    assert_shared_execution_allowed(runtime=executor.runtime, ledger=executor.ledger,
                                    workspace_id=actor.workspace_id, candidate_path=str(cwd))
    executor._executable(op)
    envelope = bounded_work({"schema_version": 1, "operation_ref": op.binding.operation_ref,
                            "input": {"type_ref": source.type_ref, "value": source.value},
                            "invocation_id": str(cog.invocation_id)})
    guard = WorkspaceLeaseGuard.exclusive_home(executor.ledger,
        WorkspaceExecutorIdentity(actor.workspace_id, agent_id, executor.runtime.image_digest),
        operation="cog.sandbox.execute", idempotency_key="cog-" + cog.invocation_id.hex)
    projections = None
    launch = None
    process = None
    stopped = False
    dispatched = False
    # Preparation has effects (placeholders); retain its outcome even on cancellation.
    prep = asyncio.create_task(asyncio.to_thread(
        binding.prepare, executor, actor, agent_id, cog.invocation_id))

    def active() -> None:
        binding.check(executor, actor)
        if cancel.is_set() or syn_fault.is_set():
            raise StateMismatchError("Worker or SYN relay is cancelled")

    def cancelled() -> bool:
        try:
            active()
            return guard.fenced
        except Exception:
            return True

    try:
        projections = await asyncio.shield(prep)
        active()
        await _settle(guard.start)
        request = ProcessRequest(
            argv=op.argv, workspace_root=executor.runtime.home, sandbox_root=executor.runtime.home,
            cwd=cwd, guest_uid=home.uid, guest_gid=home.gid, stdin=b"",
            timeout_ms=op.binding.policy.max_wall_ms,
            max_output_bytes=op.binding.policy.max_output_bytes, memory_bytes=4 * 1024**3,
            cpu_seconds=max(1, min(3600, (op.binding.policy.max_wall_ms + 999) // 1000)),
            telemetry_workspace_id=actor.workspace_id, telemetry_agent_id=agent_id,
            telemetry_identity_kind="agent", telemetry_run_id=cog.invocation_id,
        )
        supervisor = InteractiveProcessSupervisor(projections.backend)
        supervisor.monitor_config = executor.supervisor.monitor_config
        dispatched = True
        launch = asyncio.create_task(supervisor.open(request, cancelled=cancelled))
        process = await asyncio.shield(launch)
        channel = ContextWorkerChannel(
            store=ContextSessionStore(binding.database), principal=actor, agent_id=agent_id,
            guard=guard, registry=executor.homes, check_operator=active,
            work_exchange=ContextWorkExchange(envelope, op.binding.output_type_ref,
                allow_mutations=op.binding.effect_class != "read_only"))
        result = await channel.serve(process)
        stopped = result.exit.processes_stopped is True
        if not stopped:
            raise IndeterminateExecutionError("Worker shutdown was not confirmed")
        if result.exit.return_code != 0 or result.output is None:
            raise StateMismatchError("Worker did not complete its declared result protocol")
        encoded = bounded_work(result.output)
        if len(json.dumps(encoded, separators=(",", ":")).encode()) > op.binding.policy.max_output_bytes:
            raise StateMismatchError("Worker result exceeds its admitted output bound")
        active()
        await _settle(guard.ensure_live)
        output = TypedCogValue(encoded["type_ref"], encoded["value"])
        evidence = hashlib.sha256((str(result.session_id) + ":" + output.fingerprint + ":" +
                                   result.exit.isolation_identity).encode()).hexdigest()
        await _settle(lambda: guard.finish(evidence_sha256=evidence))
        return output
    finally:
        async def cleanup() -> None:
            nonlocal projections, process, stopped
            if projections is None:
                try:
                    projections = await prep
                except BaseException:
                    pass
            if launch is not None and process is None:
                try:
                    process = await launch
                except BaseException:
                    pass
            if process is not None and not stopped:
                try:
                    receipt = await process.close(force=True)
                    stopped = receipt.processes_stopped is True
                except BaseException:
                    pass
            if guard.lease is not None and not getattr(guard, "_finished", False):
                guard.abandon()  # even a stopped failed write needs ordinary outcome reconciliation
            if projections is not None and (not dispatched or stopped):
                projections.close()
            # An ambiguous launch retains immutable snapshots until host reconciliation.
        task = asyncio.create_task(cleanup())
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        await task
