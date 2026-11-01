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
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

from potbot.context.rpc_contract import ContextRequest
from potbot.context.rpc_transport import ChannelResult, serve_context_streams
from potbot.context.sessions import ContextSession, ContextSessionStore
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import IndeterminateExecutionError, ScopeDeniedError

if TYPE_CHECKING:
    from potbot.tools.agent_homes import AgentHomeRegistry
    from potbot.tools.interactive_supervisor import InteractiveExit, IsolatedInteractiveProcess
    from potbot.tools.workspace_supervision import WorkspaceLeaseGuard

_T = TypeVar("_T")


async def _settle(operation: Callable[[], _T]) -> _T:
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


class ContextWorkerChannel:
    def __init__(self, *, store: ContextSessionStore, principal: WorkspaceContext,
                 agent_id: UUID, guard: WorkspaceLeaseGuard, registry: AgentHomeRegistry,
                 check_operator: Callable[[], None]) -> None:
        if (not isinstance(principal, WorkspaceContext) or not isinstance(agent_id, UUID)
                or not callable(check_operator)):
            raise ScopeDeniedError("Context channel requires trusted host admission")
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
        if (lease is None or self.registry.ledger is not self.guard.ledger
                or lease.workspace_id != self.principal.workspace_id
                or request.workspace_id != self.principal.workspace_id
                or request.actor_id != self.agent_id):
            raise ScopeDeniedError("Context channel lease does not match the admitted agent")

    def _validate_process(self, process: IsolatedInteractiveProcess) -> UUID:
        self._authorize()
        request = process.request
        home = self.registry.lookup(self.principal.workspace_id, self.agent_id)
        if (home is None or home.workspace_id != self.principal.workspace_id
                or home.agent_id != self.agent_id or process.closed
                or not isinstance(process.isolation_identity, str)
                or not re.fullmatch(r"[0-9a-f]{64}", process.isolation_identity)
                or request.telemetry_workspace_id != self.principal.workspace_id
                or request.telemetry_agent_id != self.agent_id
                or request.telemetry_identity_kind != "agent"
                or not isinstance(request.telemetry_run_id, UUID)
                or request.guest_uid != home.uid or request.guest_gid != home.gid
                or request.stdin != b"" or request.ipc_profile != "none"
                or request.read_only_roots or request.writable_roots):
            raise ScopeDeniedError("Context worker is not the isolated registered agent")
        root = Path(self.registry.ledger.home).resolve(strict=True)
        target = root / home.directory
        if (target.is_symlink() or not target.is_dir()
                or target.resolve(strict=True).parent != root
                or Path(request.workspace_root).resolve(strict=True) != root
                or Path(request.sandbox_root or request.workspace_root).resolve(strict=True) != root
                or not Path(request.cwd).resolve(strict=True).is_relative_to(target)):
            raise ScopeDeniedError("Context worker home differs from its registration")
        pin = self.guard.ledger.image_status(self.principal.workspace_id)
        if pin is None or pin.get("digest") != self.guard.request.image_digest:
            raise ScopeDeniedError("Context worker image admission changed")
        return request.telemetry_run_id

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
        channel_task: asyncio.Task[ChannelResult] | None = None
        heartbeat: asyncio.Task[None] | None = None
        result: ChannelResult | None = None
        failure: BaseException | None = None
        cleanup: list[BaseException] = []
        try:
            run_id = await _settle(lambda: self._validate_process(process))
            grant = await _settle(lambda: self.store.issue(self.principal, self.agent_id, run_id))
            bound = grant

            def execute(request: ContextRequest) -> dict[str, Any]:
                self._authorize()
                return self.store.execute(bound, request)

            async def renew() -> None:
                while True:
                    await asyncio.sleep(15)
                    if process.closed or process.remaining_seconds() <= 0:
                        raise ScopeDeniedError("Context worker lifetime ended")
                    await _settle(self._authorize)
                    await _settle(lambda: self.store.renew(bound))

            heartbeat = asyncio.create_task(renew())
            channel_task = asyncio.create_task(serve_context_streams(
                process.reader, process.writer, execute,
            ))
            done, _ = await asyncio.wait((channel_task, heartbeat),
                                         return_when=asyncio.FIRST_COMPLETED)
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
                await asyncio.gather(*(t for t in (heartbeat, channel_task) if t is not None),
                                     return_exceptions=True)
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
                failure.add_note("Context cleanup needs host reconciliation; do not release the lease")
            raise failure
        if cleanup:
            raise IndeterminateExecutionError(
                "Context cleanup needs host reconciliation; do not release the lease"
            ) from None
        assert grant is not None and result is not None and self.exit_receipt is not None
        return ContextWorkerResult(grant.session_id, result, self.exit_receipt)
