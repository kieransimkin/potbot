"""Operator-mediated, workspace-local SYN heartbeat nodes for sandboxed Cog workers.

Every admitted Cog invocation owns a distinct runtime instance and *only* the
operator's relay can deliver its typed heartbeats.  This is deliberately a local
SYN-node protocol adapter, not an unverified external SYN network implementation.
It grants no filesystem/network authority to an agent.  Heartbeats are advisory;
only the host cgroup/pidfd/lease audit can establish process termination.

A future signed SYN transport can replace the relay without changing the wire
contract, but it must qualify identity, network policy and cross-host ordering.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError

HEARTBEAT_TYPE = "synny://type/potbot-worker-heartbeat-v1"
NODE_TYPE = "synny://type/potbot-worker-syn-node-v1"
MAX_WORKSPACE_NODES = 128
MAX_SEQUENCE = (1 << 53) - 1


@dataclass(frozen=True, slots=True)
class SynHeartbeat:
    """Operator-authenticated on the local relay, not an Internet signature."""

    workspace_id: UUID
    agent_id: UUID
    worker_run_id: UUID
    node_id: UUID
    incarnation: UUID
    sequence: int
    monotonic_ns: int
    status: Literal["admitted", "executing", "stopping"]

    def __post_init__(self) -> None:
        if not all(isinstance(getattr(self, field), UUID) for field in (
            "workspace_id", "agent_id", "worker_run_id", "node_id", "incarnation"
        )) or type(self.sequence) is not int or not 1 <= self.sequence <= MAX_SEQUENCE:
            raise InvalidInputError("SYN heartbeat lacks a bounded trusted node identity")
        if type(self.monotonic_ns) is not int or self.monotonic_ns <= 0:
            raise InvalidInputError("SYN heartbeat monotonic clock is invalid")
        if self.status not in {"admitted", "executing", "stopping"}:
            raise InvalidInputError("SYN heartbeat status is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1, "type_ref": HEARTBEAT_TYPE,
            "workspace_id": str(self.workspace_id), "agent_id": str(self.agent_id),
            "worker_run_id": str(self.worker_run_id), "node_id": str(self.node_id),
            "node_ref": f"syn://thing/potbot-worker-node-{self.node_id.hex}",
            "node_type_ref": NODE_TYPE, "incarnation": str(self.incarnation),
            "sequence": self.sequence, "monotonic_ns": self.monotonic_ns,
            "status": self.status,
        }


class SynWorkerNodeMesh:
    """Trusted, bounded relay among nodes belonging to the *same* workspace.

    The relay, rather than guest-originated JSON, binds workspace and agent IDs.
    Each node has its own heartbeat coroutine and independently maintained peer
    view.  A peer's heartbeat is not proof the associated Cog is still running.
    """

    def __init__(self, *, interval_seconds: float = 5.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not 0.05 <= interval_seconds <= 60 or not callable(clock):
            raise InvalidInputError("invalid SYN node heartbeat interval")
        self.interval = interval_seconds
        self.clock = clock
        self._nodes: dict[UUID, SynWorkerNode] = {}
        self._lock = threading.RLock()
        self._sent = 0
        self._delivered = 0

    def node(self, workspace_id: UUID, agent_id: UUID, worker_run_id: UUID) -> SynWorkerNode:
        if not all(isinstance(value, UUID) for value in (workspace_id, agent_id, worker_run_id)):
            raise ScopeDeniedError("SYN node requires host-issued workspace, agent and run IDs")
        return SynWorkerNode(self, workspace_id, agent_id, worker_run_id)

    def _register(self, node: SynWorkerNode) -> None:
        with self._lock:
            if node.node_id in self._nodes:
                raise StateMismatchError("SYN worker node has already been registered")
            if sum(p.workspace_id == node.workspace_id for p in self._nodes.values()) >= MAX_WORKSPACE_NODES:
                raise ScopeDeniedError("workspace SYN node capacity exceeded")
            self._nodes[node.node_id] = node

    def _unregister(self, node: SynWorkerNode) -> None:
        with self._lock:
            if self._nodes.get(node.node_id) is node:
                del self._nodes[node.node_id]
            for peer in self._nodes.values():
                if peer.workspace_id == node.workspace_id:
                    peer._peers.pop(node.node_id, None)

    def _broadcast(self, node: SynWorkerNode, envelope: SynHeartbeat) -> int:
        with self._lock:
            if (self._nodes.get(node.node_id) is not node
                    or envelope.node_id != node.node_id
                    or envelope.worker_run_id != node.worker_run_id
                    or envelope.agent_id != node.agent_id
                    or envelope.workspace_id != node.workspace_id
                    or envelope.incarnation != node.incarnation):
                raise ScopeDeniedError("unregistered or spoofed SYN node heartbeat")
            self._sent += 1
            delivered = 0
            for target in self._nodes.values():
                if target is node or target.workspace_id != node.workspace_id:
                    continue
                target._receive(envelope)
                delivered += 1
            self._delivered += delivered
            return delivered

    def status(self, workspace_id: UUID) -> dict[str, object]:
        """No private subprocess paths, credentials or data stream contents."""
        if not isinstance(workspace_id, UUID):
            raise ScopeDeniedError("SYN node status requires an admitted workspace UUID")
        with self._lock:
            nodes = [node for node in self._nodes.values() if node.workspace_id == workspace_id]
            return {
                "schema_version": 1, "workspace_id": str(workspace_id),
                "node_count": len(nodes),
                "nodes": [node.snapshot() for node in sorted(nodes, key=lambda n: n.node_id.hex)],
            }


class SynWorkerNode:
    """A distinct live SYN runtime *instance* for one worker invocation."""

    def __init__(self, mesh: SynWorkerNodeMesh, workspace_id: UUID,
                 agent_id: UUID, worker_run_id: UUID) -> None:
        self.mesh, self.workspace_id, self.agent_id = mesh, workspace_id, agent_id
        self.worker_run_id = worker_run_id
        self.node_id = uuid5(NAMESPACE_URL, f"potbot-syn-node:{workspace_id}:{worker_run_id}")
        self.incarnation = uuid4()
        self._sequence = 0
        self._peers: dict[UUID, tuple[SynHeartbeat, float]] = {}
        self._status: Literal["admitted", "executing", "stopping"] = "admitted"
        self._started = False
        self._ever_started = False
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._fault = threading.Event()
        self._sent = 0
        self._received = 0

    @property
    def healthy(self) -> bool:
        return self._started and not self._fault.is_set() and bool(self._task and not self._task.done())

    @property
    def fault_event(self) -> threading.Event:
        return self._fault

    def mark_executing(self) -> None:
        if not self._started or self._stop.is_set():
            raise StateMismatchError("SYN node is not admitted")
        self._status = "executing"

    def _receive(self, envelope: SynHeartbeat) -> None:
        # Only the trusted mesh calls this.  Sequence monotonicity defeats
        # duplicate/reordered frames; incarnation binds process restarts.
        if envelope.workspace_id != self.workspace_id or envelope.node_id == self.node_id:
            raise ScopeDeniedError("SYN peer heartbeat crossed its workspace boundary")
        old = self._peers.get(envelope.node_id)
        if old is not None and (
            old[0].incarnation != envelope.incarnation
            or envelope.sequence <= old[0].sequence
            or envelope.monotonic_ns < old[0].monotonic_ns
        ):
            raise StateMismatchError("SYN peer replay or incarnation conflict")
        self._peers[envelope.node_id] = (envelope, self.mesh.clock())
        self._received += 1

    def pulse(self) -> int:
        if not self._started or self._stop.is_set() or self._fault.is_set():
            raise StateMismatchError("SYN node cannot heartbeat while stopped or unhealthy")
        if self._sequence >= MAX_SEQUENCE:
            self._fault.set()
            raise StateMismatchError("SYN node heartbeat sequence exhausted")
        self._sequence += 1
        beat = SynHeartbeat(
            self.workspace_id, self.agent_id, self.worker_run_id, self.node_id,
            self.incarnation, self._sequence, max(1, int(self.mesh.clock() * 1_000_000_000)),
            self._status,
        )
        delivered = self.mesh._broadcast(self, beat)
        self._sent += 1
        return delivered

    async def _beat_loop(self) -> None:
        try:
            self.pulse()
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.mesh.interval)
                except TimeoutError:
                    self.pulse()
        except asyncio.CancelledError:
            if not self._stop.is_set():
                self._fault.set()
            raise
        except BaseException:
            self._fault.set()
            # Fail closed: controller's cancellation predicate sees this bit.

    def snapshot(self) -> dict[str, object]:
        now = self.mesh.clock()
        peers = [
            {
                "node_id": str(beat.node_id), "agent_id": str(beat.agent_id),
                "worker_run_id": str(beat.worker_run_id),
                "last_sequence": beat.sequence,
                "last_seen_seconds": round(max(0.0, now - received), 3),
                "state": "live" if now - received <= 3 * self.mesh.interval else "suspect",
            }
            for beat, received in self._peers.values()
        ]
        return {
            "node_id": str(self.node_id), "node_ref": f"syn://thing/potbot-worker-node-{self.node_id.hex}",
            "agent_id": str(self.agent_id), "worker_run_id": str(self.worker_run_id),
            "status": self._status, "healthy": self.healthy,
            "heartbeats_sent": self._sent, "peer_heartbeats_received": self._received,
            "peers": sorted(peers, key=lambda item: str(item["node_id"])),
        }

    async def __aenter__(self) -> SynWorkerNode:
        if self._ever_started:
            raise StateMismatchError("SYN node lifetime is one-use")
        self.mesh._register(self)
        self._started = True
        self._ever_started = True
        self._task = asyncio.create_task(self._beat_loop(), name=f"potbot-syn-{self.node_id.hex[:8]}")
        await asyncio.sleep(0)  # First heartbeat is emitted before Cog dispatch.
        if not self.healthy:
            await self.__aexit__(None, None, None)
            raise StateMismatchError("SYN node failed to start")
        return self

    async def __aexit__(self, *_: object) -> None:
        if not self._started:
            return
        self._status = "stopping"
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=self.mesh.interval + 1)
            except (asyncio.CancelledError, TimeoutError):
                self._fault.set()
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        self.mesh._unregister(self)
        self._started = False
