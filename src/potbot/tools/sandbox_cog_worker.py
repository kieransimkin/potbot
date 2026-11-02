"""Mandatory isolated execution of tool/work Cogs, never Python host callbacks.

Only operator-installed SandboxedCogOperation can be supplied to work Cogs.
The trusted controller validates exact semantic ports and the pinned executable,
claims a workspace lease, marshals a small typed message over stdin, executes it
under SharedWorkspaceBubblewrapBackend + the operator's audited supervisor, and
accepts a single bounded typed result. No host secrets, database object, Python
function or host filesystem path is exposed to the guest.

Controller-owned state machines (artifact leases, debugger control, signature
verification and event persistence) remain host control-plane services, NOT Cog
workers; their effects may only enter the sandbox through independently admitted
workers and are not granted arbitrary host subprocess access.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from potbot.context.runtime_integration import ContextExecutionBinding

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    CapabilityUnavailableError,
    IndeterminateExecutionError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools.agent_homes import AgentHomeRegistry
from potbot.tools.execution_contracts import WORK_KINDS, WorkBinding
from potbot.tools.released_syn_nodes import ReleasedSynWorkerNodeMesh, SynCogGrant
from potbot.tools.shared_workspace_isolation import (
    SharedWorkspaceBubblewrapBackend,
    SharedWorkspaceRuntime,
)
from potbot.tools.supervisor import ProcessRequest, ProcessSupervisor
from potbot.tools.syn_worker_nodes import SynWorkerNodeMesh
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger
from potbot.tools.workspace_supervision import (
    WorkspaceExecutorIdentity,
    WorkspaceLeaseGuard,
    assert_shared_execution_allowed,
)

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MAX_WIRE = 16 * 1024


@dataclass(frozen=True, slots=True)
class SandboxedCogOperation:
    """Trusted operator installation (never parsed from a model-produced value)."""

    binding: WorkBinding
    argv: tuple[str, ...]
    executable_sha256: str
    protocol: str = "typed-json/v1"

    def __post_init__(self) -> None:
        if self.protocol not in {"typed-json/v1", "context-rpc/v1"}:
            raise InvalidInputError("Unknown installed worker protocol")
        if self.binding.kind not in WORK_KINDS:
            raise ScopeDeniedError("sandbox Cog requires an installed work kind")
        if (
            not isinstance(self.argv, tuple)
            or not 1 <= len(self.argv) <= 32
            or any(
                not isinstance(arg, str) or not arg or len(arg) > 4096 or "\0" in arg
                for arg in self.argv
            )
            or not self.argv[0].startswith("/")
            or not _SHA.fullmatch(self.executable_sha256)
        ):
            raise InvalidInputError("sandbox Cog needs a bounded, pinned guest executable")
        if self.binding.parent_event_id or self.binding.links:
            raise ScopeDeniedError("Cog cause belongs to an invocation, not the installation")


def require_isolated_work(operations: tuple[object, ...]) -> None:
    """No arbitrary Python callbacks can be installed as executable work Cogs."""
    if not isinstance(operations, tuple) or any(
        not isinstance(op, SandboxedCogOperation) for op in operations
    ):
        raise ScopeDeniedError(
            "Cog work requires a qualified sandbox installation; host callbacks are forbidden"
        )


def _one_json(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > _MAX_WIRE:
        raise StateMismatchError("sandboxed Cog output is absent or exceeds the typed bound")

    def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise StateMismatchError("sandbox Cog output contains duplicate keys")
            result[key] = value
        return result

    try:
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite")),
        )
    except (UnicodeError, ValueError) as exc:
        raise StateMismatchError("sandboxed Cog result is not a canonical JSON object") from exc
    if (
        not isinstance(data, dict)
        or set(data) != {"schema_version", "type_ref", "value"}
        or data["schema_version"] != 1
    ):
        raise StateMismatchError("sandboxed Cog returned an undeclared result envelope")
    return data


class SandboxedCogExecutor:
    """Host-only runner; scoped to ONE qualified workspace image/agent registry."""

    def __init__(
        self,
        runtime: SharedWorkspaceRuntime,
        ledger: WorkspaceLeaseLedger,
        homes: AgentHomeRegistry,
        supervisor: ProcessSupervisor,
        syn_mesh: SynWorkerNodeMesh,
        *,
        development_syn_fixture: bool = False,
    ) -> None:
        if (
            not isinstance(runtime, SharedWorkspaceRuntime)
            or not isinstance(ledger, WorkspaceLeaseLedger)
            or not isinstance(homes, AgentHomeRegistry)
            or homes.ledger is not ledger
            or not isinstance(supervisor, ProcessSupervisor)
            or not isinstance(supervisor.backend, SharedWorkspaceBubblewrapBackend)
            or supervisor.backend.runtime != runtime
            or not isinstance(syn_mesh, SynWorkerNodeMesh)
            or (
                not isinstance(syn_mesh, ReleasedSynWorkerNodeMesh)
                and development_syn_fixture is not True
            )
        ):
            raise ScopeDeniedError("Cog runner requires the qualified pinned host sandbox")
        self.runtime, self.ledger, self.homes, self.supervisor = runtime, ledger, homes, supervisor
        self.syn_mesh = syn_mesh
        self.development_syn_fixture = development_syn_fixture
        self._context_binding: ContextExecutionBinding | None = None

    def syn_probe(self, actor: WorkspaceContext, agent_id: UUID) -> None:
        if isinstance(self.syn_mesh, ReleasedSynWorkerNodeMesh):
            self.syn_mesh.probe(actor.workspace_id, agent_id)
        elif getattr(self, "development_syn_fixture", False) is not True:
            raise CapabilityUnavailableError(
                "Cog dispatch requires independently released native Syn nodes"
            )

    def authorize_syn(
        self,
        op: SandboxedCogOperation,
        source: TypedCogValue,
        context: CogRunContext,
        actor: WorkspaceContext,
        agent_id: UUID,
    ) -> SynCogGrant | None:
        self.syn_probe(actor, agent_id)
        if isinstance(self.syn_mesh, ReleasedSynWorkerNodeMesh):
            return self.syn_mesh.authorize(op, source, context, actor, agent_id)
        return None  # Explicit development fixture, never production qualification.

    def syn_provenance(self, actor: WorkspaceContext, agent_id: UUID) -> dict[str, Any] | None:
        self.syn_probe(actor, agent_id)
        if isinstance(self.syn_mesh, ReleasedSynWorkerNodeMesh):
            return self.syn_mesh.transport.provenance(agent_id)
        return None

    def bind_context(
        self, database: Any, actor: WorkspaceContext, operations: tuple[SandboxedCogOperation, ...]
    ) -> None:
        """Called by the trusted controller after its ordinary installation checks."""
        from potbot.context.runtime_integration import ContextExecutionBinding

        prior = self._context_binding
        if prior is not None and (prior.database is not database or prior.principal != actor):
            raise ScopeDeniedError("A context executor cannot be rebound across principals")
        selected = prior or ContextExecutionBinding(database, actor)
        selected.install(operations)
        self._context_binding = selected

    def _executable(self, op: SandboxedCogOperation) -> None:
        guest = op.argv[0]
        if guest.startswith("/usr/local/tools/"):
            registered = dict(self.runtime.tool_entrypoints)
            if registered.get(guest) != op.executable_sha256:
                raise ScopeDeniedError("Cog shared-tool executable has no pinned admission")
            base, relative = self.runtime.shared_tools, guest.removeprefix("/usr/local/tools/")
            if base is None:
                raise CapabilityUnavailableError("shared tool store is unavailable")
        else:
            base, relative = self.runtime.rootfs, guest.lstrip("/")
        candidate = Path(base)
        if not candidate.is_dir() or candidate.is_symlink():
            raise ScopeDeniedError("operator executable source is not a qualified directory")
        root = candidate.resolve(strict=True)
        for component in Path(relative).parts:
            if component in {"", ".", ".."}:
                raise ScopeDeniedError("Cog executable escapes pinned image")
            candidate = candidate / component
            if candidate.is_symlink():
                raise ScopeDeniedError("Cog executable may not follow symlinks")
        binary = candidate.resolve(strict=True)
        if (
            not binary.is_file()
            or not binary.is_relative_to(root)
            or not os.access(binary, os.X_OK)
        ):
            raise ScopeDeniedError("Cog executable is not present in admitted image")
        with binary.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != op.executable_sha256:
                raise StateMismatchError("Cog worker binary changed after installation")

    def _run_sync(
        self,
        op: SandboxedCogOperation,
        source: TypedCogValue,
        cog_context: CogRunContext,
        actor: WorkspaceContext,
        agent_id: UUID,
        cancel: threading.Event,
        syn_fault: threading.Event | None = None,
    ) -> TypedCogValue:
        home = self.homes.lookup(actor.workspace_id, agent_id)
        if home is None or home.workspace_id != actor.workspace_id or home.agent_id != agent_id:
            raise ScopeDeniedError("Cog worker agent identity is not registered")
        cwd = self.runtime.home / home.directory
        assert_shared_execution_allowed(
            runtime=self.runtime,
            ledger=self.ledger,
            workspace_id=actor.workspace_id,
            candidate_path=str(cwd),
        )
        self._executable(op)
        wire = canonical_json(
            {
                "schema_version": 1,
                "operation_ref": op.binding.operation_ref,
                "input": {"type_ref": source.type_ref, "value": source.value},
                "invocation_id": str(cog_context.invocation_id),
            }
        )
        stdin = wire.encode("utf-8")
        if len(stdin) > _MAX_WIRE:
            raise InvalidInputError("Cog input must fit its admitted typed transport")
        identity = WorkspaceExecutorIdentity(
            actor.workspace_id, agent_id, self.runtime.image_digest
        )
        guard = WorkspaceLeaseGuard.exclusive_home(
            self.ledger,
            identity,
            operation="cog.sandbox.execute",
            idempotency_key="cog-" + cog_context.invocation_id.hex,
        )
        projections = None
        selected_supervisor = self.supervisor
        if self._context_binding is not None:
            projections = self._context_binding.prepare(
                self, actor, agent_id, cog_context.invocation_id
            )
            selected_supervisor = ProcessSupervisor(projections.backend)
            selected_supervisor.monitor_config = self.supervisor.monitor_config
        stopped = False
        dispatched = False
        try:
            guard.start()
            request = ProcessRequest(
                argv=op.argv,
                workspace_root=self.runtime.home,
                sandbox_root=self.runtime.home,
                cwd=cwd,
                guest_uid=home.uid,
                guest_gid=home.gid,
                stdin=stdin,
                timeout_ms=op.binding.policy.max_wall_ms,
                max_output_bytes=op.binding.policy.max_output_bytes,
                memory_bytes=4 * 1024**3,
                cpu_seconds=max(1, min(3600, (op.binding.policy.max_wall_ms + 999) // 1000)),
                telemetry_workspace_id=actor.workspace_id,
                telemetry_agent_id=agent_id,
                telemetry_identity_kind="agent",
                telemetry_run_id=cog_context.invocation_id,
            )
            dispatched = True
            result = selected_supervisor.run(
                request,
                cancelled=lambda: (
                    cancel.is_set()
                    or guard.fenced
                    or (syn_fault is not None and syn_fault.is_set())
                    or (self._context_binding is not None and not self._context_live(actor))
                ),
            )
            stopped = result.processes_stopped is True
            if not stopped or result.outcome == "inconclusive":
                raise IndeterminateExecutionError(
                    "sandbox Cog process tree or audit is not confirmed stopped"
                )
            if result.outcome != "completed" or result.exit_code != 0 or result.stdout_truncated:
                raise StateMismatchError("sandbox Cog did not return an admitted typed result")
            response = _one_json(result.stdout)
            output = TypedCogValue(response["type_ref"], response["value"])
            if output.type_ref != op.binding.output_type_ref:
                raise StateMismatchError("sandbox Cog changed its registered semantic output port")
            guard.ensure_live()
            if syn_fault is not None and syn_fault.is_set():
                raise IndeterminateExecutionError("worker SYN node failed during sandbox execution")
            self.syn_probe(actor, agent_id)
            guard.finish(
                evidence_sha256=content_hash(
                    {
                        "run": str(cog_context.invocation_id),
                        "result": output.fingerprint,
                        "isolation": result.isolation_identity,
                    }
                )
            )
            return output
        except BaseException:
            # Never release a lease after possibly dispatched and unconfirmed effects.
            try:
                if dispatched:
                    guard.finish(
                        evidence_sha256=content_hash(
                            {"attempt": str(cog_context.invocation_id), "state": "indeterminate"}
                        ),
                        uncertain=True,
                    )
                else:
                    guard.finish(
                        evidence_sha256=content_hash(
                            {"attempt": str(cog_context.invocation_id), "state": "not_dispatched"}
                        )
                    )
            except Exception:
                guard.abandon()
            raise
        finally:
            if projections is not None and (not dispatched or stopped):
                projections.close()

    def _context_live(self, actor: WorkspaceContext) -> bool:
        try:
            binding = self._context_binding
            if binding is None:
                return False
            binding.check(self, actor)
            return True
        except Exception:
            return False

    async def execute(
        self,
        op: SandboxedCogOperation,
        source: TypedCogValue,
        cog_context: CogRunContext,
        actor: WorkspaceContext,
        agent_id: UUID,
        *,
        syn_authorization: SynCogGrant | None = None,
    ) -> TypedCogValue:
        if not isinstance(op, SandboxedCogOperation) or not isinstance(agent_id, UUID):
            raise ScopeDeniedError("Cog worker requires trusted installed operation and agent")
        if source.type_ref != op.binding.input_type_ref:
            raise ScopeDeniedError("Cog work source differs from pinned typed port")
        # Separate SYN node runtime instance for each admitted worker. The
        # operator-only mesh relays heartbeats within the workspace; it never
        # opens guest-to-guest sockets or grants a capability to the worker.
        if not isinstance(getattr(self, "syn_mesh", None), SynWorkerNodeMesh):
            raise CapabilityUnavailableError("worker SYN node relay must be configured")
        self.syn_probe(actor, agent_id)
        if isinstance(self.syn_mesh, ReleasedSynWorkerNodeMesh):
            self.syn_mesh.consume(syn_authorization, op, source, cog_context, actor, agent_id)
        cancelled = threading.Event()
        async with self.syn_mesh.node(
            actor.workspace_id, agent_id, cog_context.invocation_id
        ) as node:
            node.mark_executing()
            if op.protocol == "context-rpc/v1":
                from potbot.context.runtime_integration import execute_context_operation

                job = asyncio.create_task(
                    execute_context_operation(
                        self, op, source, cog_context, actor, agent_id, cancelled, node.fault_event
                    )
                )
            else:
                job = asyncio.create_task(
                    asyncio.to_thread(
                        self._run_sync,
                        op,
                        source,
                        cog_context,
                        actor,
                        agent_id,
                        cancelled,
                        node.fault_event,
                    )
                )
            try:
                result: TypedCogValue = await asyncio.shield(job)
                try:
                    self.syn_probe(actor, agent_id)
                    if not node.healthy:
                        raise StateMismatchError(
                            "Native Syn monitor failed before result acceptance"
                        )
                except Exception:
                    raise IndeterminateExecutionError(
                        "Cog effects require reconciliation after Syn loss"
                    ) from None
                return result
            except asyncio.CancelledError:
                cancelled.set()
                # Never confuse an interrupted controller task with a stopped
                # process. The underlying supervisor must finish its audit.
                try:
                    await asyncio.wait_for(asyncio.shield(job), timeout=5)
                except (TimeoutError, asyncio.CancelledError, Exception):
                    pass
                raise IndeterminateExecutionError(
                    "Cog task cancelled; sandbox process reconciliation required"
                ) from None
            finally:
                if not job.done():
                    cancelled.set()
