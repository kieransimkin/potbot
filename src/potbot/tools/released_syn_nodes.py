"""Cog admission and heartbeats over independently installed native Syn nodes.

The controller issues one-use task grants after its normal admission checks.
Syn authenticates transport; neither a heartbeat nor Syn readiness grants work.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import content_hash
from potbot.syn_release.mesh import SynPrivateMesh
from potbot.tools.syn_worker_nodes import (
    HEARTBEAT_TYPE,
    SynHeartbeat,
    SynWorkerNode,
    SynWorkerNodeMesh,
)

if TYPE_CHECKING:
    from potbot.tools.sandbox_cog_worker import SandboxedCogOperation


@dataclass(frozen=True, slots=True)
class SynCogGrant:
    """Opaque host reference, never a model-supplied or serializable capability."""

    token: UUID


class ReleasedSynWorkerNodeMesh(SynWorkerNodeMesh):
    """One fixed native cohort; logical invocation nodes share its transport."""

    def __init__(self, transport: SynPrivateMesh, *, interval_seconds: float = 5.0) -> None:
        if not isinstance(transport, SynPrivateMesh):
            raise ScopeDeniedError("Cog mesh needs the released native Syn adapter")
        super().__init__(interval_seconds=interval_seconds)
        self.transport = transport
        self._grants: dict[UUID, tuple[SynCogGrant, str, float]] = {}
        self._retired: set[tuple[UUID, UUID]] = set()
        self._closed = threading.Event()
        self._fault = threading.Event()
        self._reader: threading.Thread | None = None

    def _live(self) -> None:
        if (
            self._reader is None
            or not self._reader.is_alive()
            or self._closed.is_set()
            or self._fault.is_set()
            or not self.transport.healthy
        ):
            raise StateMismatchError("Released Syn cohort is unavailable; work remains fenced")

    def probe(self, workspace: UUID, agent: UUID, *, installation: bool = False) -> None:
        self._live()
        if workspace != self.transport.workspace_id:
            raise ScopeDeniedError("Cog workspace differs from its admitted native Syn cohort")
        self.transport.probe(agent, verify_installation=installation)

    def _binding(
        self,
        op: SandboxedCogOperation,
        source: TypedCogValue,
        context: CogRunContext,
        actor: WorkspaceContext,
        agent: UUID,
    ) -> str:
        selected = self.transport.participant(agent)
        return content_hash(
            {
                "workspace": str(actor.workspace_id),
                "principal": str(actor.principal_id),
                "agent": str(agent),
                "context": {key: str(value) for key, value in asdict(context).items()},
                "operation": op.binding.to_dict(),
                "argv": op.argv,
                "executable": op.executable_sha256,
                "protocol": op.protocol,
                "input": source.fingerprint,
                "session": self.transport.scope["session_id"],
                "node": selected.selected_hash,
                "boot": selected.proof["status"]["boot_id"],
            }
        )

    def authorize(
        self,
        op: SandboxedCogOperation,
        source: TypedCogValue,
        context: CogRunContext,
        actor: WorkspaceContext,
        agent: UUID,
    ) -> SynCogGrant:
        """Trusted ExecutionCogRuntime calls this only AFTER ordinary task admission."""
        self.probe(actor.workspace_id, agent, installation=True)
        binding = self._binding(op, source, context, actor, agent)
        now = time.monotonic()
        with self._lock:
            self._grants = {key: value for key, value in self._grants.items() if value[2] > now}
            if len(self._grants) >= 128:
                raise ScopeDeniedError("Too many outstanding one-use Cog grants")
            grant = SynCogGrant(uuid4())
            self._grants[grant.token] = (grant, binding, now + 10)
        return grant

    def consume(
        self,
        grant: SynCogGrant | None,
        op: SandboxedCogOperation,
        source: TypedCogValue,
        context: CogRunContext,
        actor: WorkspaceContext,
        agent: UUID,
    ) -> None:
        with self._lock:
            issued = self._grants.pop(grant.token, None) if isinstance(grant, SynCogGrant) else None
        if (
            issued is None
            or issued[0] is not grant
            or issued[2] <= time.monotonic()
            or issued[1] != self._binding(op, source, context, actor, agent)
        ):
            raise ScopeDeniedError("Cog grant is absent, changed, expired or already consumed")
        self.probe(actor.workspace_id, agent)

    def node(self, workspace_id: UUID, agent_id: UUID, worker_run_id: UUID) -> SynWorkerNode:
        self.probe(workspace_id, agent_id)
        return ReleasedSynWorkerNode(self, workspace_id, agent_id, worker_run_id)

    def _register(self, node: SynWorkerNode) -> None:
        self.probe(node.workspace_id, node.agent_id)
        super()._register(node)

    def _unregister(self, node: SynWorkerNode) -> None:
        with self._lock:
            self._retired.add((node.node_id, node.incarnation))
            if len(self._retired) > 4096:
                self._fault.set()  # Reconcile the workspace; never silently reuse a run.
        super()._unregister(node)

    def _broadcast(self, node: SynWorkerNode, envelope: SynHeartbeat) -> int:
        self._live()
        with self._lock:
            if (
                self._nodes.get(node.node_id) is not node
                or envelope.workspace_id != self.transport.workspace_id
                or envelope.agent_id != node.agent_id
                or envelope.node_id != node.node_id
                or envelope.worker_run_id != node.worker_run_id
                or envelope.incarnation != node.incarnation
            ):
                raise ScopeDeniedError("Unregistered or spoofed native Syn heartbeat")
        self.transport.call(
            node.agent_id, "publish", {"topic": HEARTBEAT_TYPE, "value": envelope.to_dict()}
        )
        with self._lock:
            self._sent += 1
        return 0  # Publication ACK is not evidence of peer reception.

    def _receive_native(self, recipient: UUID, entry: dict[str, Any]) -> None:
        header, payload = entry["envelope"]["header"], entry["payload"]
        if header["topic"] != HEARTBEAT_TYPE:
            raise ScopeDeniedError("This Cog monitor admits only its typed heartbeat topic")
        agent = UUID(payload["agent_id"])
        selected = self.transport.participant(agent)
        if (
            header["node"] != selected.selected_hash
            or header["boot_id"] != selected.proof["status"]["boot_id"]
            or header["workspace_id"] != str(self.transport.workspace_id)
            or agent == recipient
        ):
            raise ScopeDeniedError("Heartbeat claims another enrolled Agent, boot or workspace")
        beat = SynHeartbeat(
            UUID(payload["workspace_id"]),
            agent,
            UUID(payload["worker_run_id"]),
            UUID(payload["node_id"]),
            UUID(payload["incarnation"]),
            payload["sequence"],
            payload["monotonic_ns"],
            payload["status"],
        )
        if payload != beat.to_dict():
            raise ScopeDeniedError("Heartbeat differs from its exact typed contract")
        with self._lock:
            source = self._nodes.get(beat.node_id)
            if source is None and (beat.node_id, beat.incarnation) in self._retired:
                return
            if (
                source is None
                or source.agent_id != agent
                or source.worker_run_id != beat.worker_run_id
                or source.workspace_id != beat.workspace_id
                or source.incarnation != beat.incarnation
            ):
                raise ScopeDeniedError("Heartbeat has no active operator-admitted invocation")
            for target in self._nodes.values():
                if target.agent_id == recipient and target.workspace_id == beat.workspace_id:
                    target._receive(beat)
                    self._delivered += 1

    def _drain(self) -> None:
        try:
            while not self._closed.is_set():
                if not self.transport.healthy:
                    raise StateMismatchError("Native Syn process or relay stopped")
                for agent in self.transport.agents:
                    if self._closed.is_set():
                        break
                    received = self.transport.call(agent, "receive")
                    for entry in received["reply"]["result"]["messages"]:
                        self._receive_native(agent, entry)
                self._closed.wait(0.1)
        except BaseException:
            self._fault.set()
        finally:
            with self._lock:
                for node in self._nodes.values():
                    node.fault_event.set()

    @contextmanager
    def running(self) -> Iterator[ReleasedSynWorkerNodeMesh]:
        if self._reader is not None or self._closed.is_set() or not self.transport.healthy:
            raise StateMismatchError(
                "Native Cog mesh lifetime is one-use and requires a live relay"
            )
        self._reader = threading.Thread(
            target=self._drain, name="potbot-syn-heartbeats", daemon=True
        )
        self._reader.start()
        try:
            self._live()
            yield self
        finally:
            self._closed.set()
            self._reader.join(timeout=15)
            if self._reader.is_alive():
                raise StateMismatchError(
                    "Native heartbeat consumer shutdown requires reconciliation"
                )
            self._fault.set()
            with self._lock:
                self._grants.clear()
                for node in self._nodes.values():
                    node.fault_event.set()


class ReleasedSynWorkerNode(SynWorkerNode):
    """Logical per-Cog monitor, authenticated by the Agent's persistent Syn node."""

    async def _beat_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.mesh.interval)
                except TimeoutError:
                    await asyncio.to_thread(self.pulse)
        except BaseException:
            self._fault.set()

    async def __aenter__(self) -> SynWorkerNode:
        if self._ever_started:
            raise StateMismatchError("Syn invocation monitor is one-use")
        self.mesh._register(self)
        self._started = self._ever_started = True
        try:
            await asyncio.to_thread(self.pulse)
            self._task = asyncio.create_task(self._beat_loop())
            if self._fault.is_set():
                raise StateMismatchError("Syn heartbeat reception failed before Cog dispatch")
            return self
        except BaseException:
            self._fault.set()
            self.mesh._unregister(self)
            self._started = False
            raise
