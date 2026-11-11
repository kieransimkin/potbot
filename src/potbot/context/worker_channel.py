"""Bind context RPC to an already-isolated, already-admitted agent process.

This adapter never launches an executable or creates a mount/network listener.
The trusted dispatcher supplies its registered agent, live process lease and
interactive process pipes. It must retain that lease until our exit receipt
confirms process shutdown. Guests only send the bounded RPC contract.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.context.rpc_contract import OperationRequest, WorkRequest
from potbot.context.rpc_transport import ChannelResult, serve_context_streams
from potbot.context.sessions import ContextSession, ContextSessionStore
from potbot.context.syn_context_route import SynContextInvocation, SynContextRoute, WorkerRpcBinding
from potbot.context.work_exchange import ContextWorkExchange
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import IndeterminateExecutionError, ScopeDeniedError

if TYPE_CHECKING:
    from potbot.tools.agent_homes import AgentHomeRegistry
    from potbot.tools.interactive_supervisor import InteractiveExit, IsolatedInteractiveProcess
    from potbot.tools.workspace_supervision import WorkspaceLeaseGuard


async def _settle[T](operation: Callable[[], T]) -> T:
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise


@dataclass(frozen=True, slots=True)
class ContextWorkerResult:
    session_id: UUID
    channel: ChannelResult
    exit: InteractiveExit
    output: dict[str, Any] | None = None


class ContextWorkerChannel:
    def __init__(
        self,
        *,
        store: ContextSessionStore,
        principal: WorkspaceContext,
        agent_id: UUID,
        guard: WorkspaceLeaseGuard,
        registry: AgentHomeRegistry,
        check_operator: Callable[[], None],
        work_exchange: ContextWorkExchange | None = None,
        syn_route: SynContextRoute | None = None,
        syn_agent: Callable[[], tuple[str, str]] | None = None,
    ) -> None:
        if (
            not isinstance(principal, WorkspaceContext)
            or not isinstance(agent_id, UUID)
            or not callable(check_operator)
        ):
            raise ScopeDeniedError("Context channel requires trusted host admission")
        if work_exchange is not None and not isinstance(work_exchange, ContextWorkExchange):
            raise ScopeDeniedError("Work exchange must be host-bound")
        if (syn_route is None) != (syn_agent is None):
            raise ScopeDeniedError("Syn Context routing requires a live native Agent pin")
        self.syn_route, self.syn_agent = syn_route, syn_agent
        self.work_exchange = work_exchange
        self.store, self.principal, self.agent_id = store, principal, agent_id
        self.guard, self.registry, self.check_operator = guard, registry, check_operator
        self.exit_receipt: InteractiveExit | None = None
        self._used = False

    def _authorize(self) -> None:
        # The callback is supplied by host code (for example operator.monitor.require_active),
        # never by JSON. False must not accidentally mean successful admission.
        if self.check_operator() is not None:
            raise ScopeDeniedError("Operator admission must fail by raising")
        self.guard.ensure_live()
        lease, request = self.guard.lease, self.guard.request
        if (
            lease is None
            or self.registry.ledger is not self.guard.ledger
            or lease.workspace_id != self.principal.workspace_id
            or request.workspace_id != self.principal.workspace_id
            or request.actor_id != self.agent_id
        ):
            raise ScopeDeniedError("Context channel lease does not match the admitted agent")

    def _validate_process(self, process: IsolatedInteractiveProcess) -> UUID:
        self._authorize()
        if (
            process.closed
            or not isinstance(process.isolation_identity, str)
            or not re.fullmatch(r"[0-9a-f]{64}", process.isolation_identity)
        ):
            raise ScopeDeniedError("Context worker has no live isolation receipt")
        home = self.registry.lookup(self.principal.workspace_id, self.agent_id)
        run_id = validate_worker_binding(
            process.request, home, Path(self.registry.ledger.home), self.principal, self.agent_id
        )
        pin = self.guard.ledger.image_status(self.principal.workspace_id)
        if pin is None or pin.get("digest") != self.guard.request.image_digest:
            raise ScopeDeniedError("Context worker image admission changed")
        return run_id

    async def serve(self, process: IsolatedInteractiveProcess) -> ContextWorkerResult:
        """Single-use; close process and revoke session on EOF, timeout, or error.

        Call after InteractiveProcessSupervisor.open with the same trusted
        ProcessRequest used for lease/agent admission. Do not multiplex another
        stdout protocol on this worker. The caller owns guard.finish/abandon.
        """
        if self._used:
            raise ScopeDeniedError("Context worker channel cannot be reused")
        self._used = True
        grant: ContextSession | None = None
        invocation: SynContextInvocation | None = None
        channel_task: asyncio.Task[ChannelResult] | None = None
        heartbeat: asyncio.Task[None] | None = None
        result: ChannelResult | None = None
        failure: BaseException | None = None
        cleanup: list[BaseException] = []
        try:
            run_id = await _settle(lambda: self._validate_process(process))
            await _settle(lambda: self.store.ensure_agent_context(self.principal, self.agent_id))

            def issue() -> None:
                nonlocal grant
                grant = self.store.issue(self.principal, self.agent_id, run_id)

            await _settle(issue)
            assert grant is not None
            bound = grant

            def execute(request: OperationRequest) -> dict[str, Any]:
                self._authorize()
                if self.work_exchange is not None:
                    self.work_exchange.admit(request)
                if isinstance(request, WorkRequest):
                    if self.work_exchange is None:
                        raise ScopeDeniedError("This worker has no installed typed work port")
                    return self.work_exchange.execute(self.store, bound, request)
                return self.store.execute(bound, request)

            async def renew() -> None:
                while True:
                    await asyncio.sleep(15)
                    if process.closed or process.remaining_seconds() <= 0:
                        raise ScopeDeniedError("Context worker lifetime ended")
                    await _settle(self._authorize)
                    await _settle(lambda: self.store.renew(bound))

            if self.syn_route is not None:
                assert self.syn_agent is not None
                native_agent = self.syn_agent

                def capture() -> WorkerRpcBinding:
                    if (
                        self._validate_process(process) != run_id
                        or process.remaining_seconds() <= 0
                    ):
                        raise ScopeDeniedError("Context worker invocation changed or ended")
                    lease = self.guard.lease
                    assert lease is not None
                    key, boot = native_agent()
                    return WorkerRpcBinding(
                        self.principal.workspace_id,
                        self.agent_id,
                        run_id,
                        key,
                        boot,
                        process.isolation_identity,
                        lease.lease_id,
                        lease.generation,
                    )

                def still_live(expected: WorkerRpcBinding) -> None:
                    if capture() != expected:
                        raise ScopeDeniedError("Context worker, native Agent or lease changed")

                route = self.syn_route

                def bind_invocation() -> None:
                    nonlocal invocation
                    invocation = route.bind(capture(), still_live, execute)

                await _settle(bind_invocation)

            heartbeat = asyncio.create_task(renew())
            if self.syn_route is None:
                channel_task = asyncio.create_task(
                    serve_context_streams(process.reader, process.writer, execute)
                )
            else:
                assert invocation is not None
                channel_task = asyncio.create_task(
                    serve_context_streams(process.reader, process.writer, forward=invocation.call)
                )
            done, _ = await asyncio.wait(
                (channel_task, heartbeat), return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in done:
                heartbeat.result()  # fail closed on the first failed independent lease check
            result = await channel_task
        except BaseException as exc:
            failure = exc
        finally:

            async def cleanup_all() -> None:
                for task in (heartbeat, channel_task):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(
                    *(t for t in (heartbeat, channel_task) if t is not None), return_exceptions=True
                )
                # A Syn delivery may have reached the destination even after
                # the originating socket times out. Settle it before revocation.
                if invocation is not None:
                    try:
                        await _settle(invocation.close)
                    except BaseException as exc:
                        cleanup.append(exc)
                # No DB mutation remains detached when we get here.
                try:
                    self.exit_receipt = await process.close(force=failure is not None)
                    if not self.exit_receipt.processes_stopped:
                        raise IndeterminateExecutionError("Context worker shutdown is unconfirmed")
                except BaseException as exc:
                    cleanup.append(exc)
                if grant is not None:
                    try:
                        issued = grant
                        await _settle(lambda: self.store.revoke(issued))
                    except BaseException as exc:
                        cleanup.append(exc)

            cleanup_task = asyncio.create_task(cleanup_all())
            # Repeated task cancellation must not detach process teardown or revocation.
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError as exc:
                    if failure is None:
                        failure = exc
            try:
                cleanup_task.result()
            except BaseException as exc:
                cleanup.append(exc)
        if failure is not None:
            if cleanup:
                failure.add_note(
                    "Context cleanup needs host reconciliation; do not release the lease"
                )
            raise failure
        if cleanup:
            raise IndeterminateExecutionError(
                "Context cleanup needs host reconciliation; do not release the lease"
            ) from None
        assert grant is not None and result is not None and self.exit_receipt is not None
        return ContextWorkerResult(
            grant.session_id,
            result,
            self.exit_receipt,
            self.work_exchange.result if self.work_exchange is not None else None,
        )


def validate_worker_binding(
    request: Any, home: Any, root: Path, principal: WorkspaceContext, agent_id: UUID
) -> UUID:
    """Validate identity/home/pipe policy before spawn AND again before RPC issuance."""
    if (
        home is None
        or home.workspace_id != principal.workspace_id
        or home.agent_id != agent_id
        or request.telemetry_workspace_id != principal.workspace_id
        or request.telemetry_agent_id != agent_id
        or request.telemetry_identity_kind != "agent"
        or not isinstance(request.telemetry_run_id, UUID)
        or request.guest_uid != home.uid
        or request.guest_gid != home.gid
        or request.stdin != b""
        or request.ipc_profile != "none"
        or request.read_only_roots
        or request.writable_roots
    ):
        raise ScopeDeniedError("Context worker is not the isolated registered agent")
    root = root.resolve(strict=True)
    target = root / home.directory
    if (
        target.is_symlink()
        or not target.is_dir()
        or target.resolve(strict=True).parent != root
        or Path(request.workspace_root).resolve(strict=True) != root
        or Path(request.sandbox_root or request.workspace_root).resolve(strict=True) != root
        or not Path(request.cwd).resolve(strict=True).is_relative_to(target)
    ):
        raise ScopeDeniedError("Context worker home differs from its registration")
    return request.telemetry_run_id
