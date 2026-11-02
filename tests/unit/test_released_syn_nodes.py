"""Admission/monitor fault tests with an explicit public-transport double.

Native crypto, custody, process identity and release installation are exercised
separately by Syn's two-node source and published-package qualification.
"""

from __future__ import annotations

import asyncio
import copy
import queue
import time
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import CapabilityUnavailableError, ScopeDeniedError, StateMismatchError
from potbot.syn_release.mesh import SynPrivateMesh
from potbot.tools.execution_contracts import ExecutionPolicy, WorkBinding
from potbot.tools.released_syn_nodes import ReleasedSynWorkerNodeMesh, SynCogGrant
from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor, SandboxedCogOperation
from potbot.tools.syn_worker_nodes import HEARTBEAT_TYPE, SynWorkerNodeMesh


class PublicTransportDouble(SynPrivateMesh):
    def __init__(self):
        self.workspace_id = uuid4()
        self.scope = {"session_id": "1" * 32}
        self._agents = (uuid4(), uuid4())
        self.selected = {
            agent: SimpleNamespace(
                selected_hash=str(index) * 64, proof={"status": {"boot_id": "b" * 32}}
            )
            for index, agent in enumerate(self._agents, 1)
        }
        self.queues = {agent: queue.Queue() for agent in self.agents}
        self.live = True
        self.probes = []

    @property
    def agents(self):
        return self._agents

    @property
    def healthy(self):
        return self.live

    def participant(self, agent):
        if agent not in self.selected:
            raise ScopeDeniedError("outside fixture cohort")
        return self.selected[agent]

    def probe(self, agent, *, verify_installation=False):
        self.participant(agent)
        if not self.live:
            raise StateMismatchError("fixture native node unavailable")
        self.probes.append((agent, verify_installation))
        return {}

    def provenance(self, agent):
        return {"manifest_sha256": "a" * 64, "source_commit": "b" * 40, "version": "0.4.0"}

    def call(self, agent, operation, payload=None):
        self.probe(agent)
        if operation == "publish":
            source = self.participant(agent)
            entry = {
                "envelope": {
                    "header": {
                        "node": source.selected_hash,
                        "boot_id": source.proof["status"]["boot_id"],
                        "topic": payload["topic"],
                        "workspace_id": str(self.workspace_id),
                    }
                },
                "payload": copy.deepcopy(payload["value"]),
            }
            for peer in self.agents:
                if peer != agent:
                    self.queues[peer].put(entry)
            return {"reply": {"result": {"relay_accepted": True, "peer_reception_proved": False}}}
        assert operation == "receive"
        messages = []
        for _ in range(2):
            try:
                messages.append(self.queues[agent].get_nowait())
            except queue.Empty:
                break
        return {"reply": {"result": {"messages": messages}}}


def inputs(transport):
    actor = WorkspaceContext(transport.workspace_id, uuid4())
    policy = ExecutionPolicy("a" * 64, "b" * 64, "c" * 64, "unit-double")
    op = SandboxedCogOperation(
        WorkBinding(
            "tool",
            "syn://thing/test-worker",
            "syn://type/in",
            "syn://type/out",
            "d" * 64,
            "read_only",
            policy,
        ),
        ("/usr/bin/fixture",),
        "e" * 64,
    )
    source = TypedCogValue("syn://type/in", {"value": 1})
    context = CogRunContext("f" * 64, uuid4(), uuid4(), uuid4())
    return op, source, context, actor, transport.agents[0]


@pytest.mark.parametrize(
    "mutation",
    [
        "input",
        "task",
        "repository",
        "snapshot",
        "invocation",
        "agent",
        "workspace",
        "principal",
        "binary",
        "boot",
        "session",
        "expired",
        "forged",
        "missing",
    ],
)
def test_one_use_cog_grant_rejects_scope_changes(mutation):
    transport = PublicTransportDouble()
    mesh = ReleasedSynWorkerNodeMesh(transport)
    with mesh.running():
        op, source, context, actor, agent = inputs(transport)
        grant = mesh.authorize(op, source, context, actor, agent)
        assert (agent, True) in transport.probes
        if mutation == "input":
            source = TypedCogValue("syn://type/in", {"value": 2})
        elif mutation in {"task", "repository", "invocation"}:
            field = {
                "task": "task_revision_id",
                "repository": "repository_state_id",
                "invocation": "invocation_id",
            }[mutation]
            context = replace(context, **{field: uuid4()})
        elif mutation == "snapshot":
            context = replace(context, cog_snapshot_hash="0" * 64)
        elif mutation == "agent":
            agent = transport.agents[1]
        elif mutation in {"workspace", "principal"}:
            actor = replace(actor, **{mutation + "_id": uuid4()})
        elif mutation == "binary":
            op = replace(op, executable_sha256="0" * 64)
        elif mutation == "boot":
            transport.selected[agent].proof["status"]["boot_id"] = "0" * 32
        elif mutation == "session":
            transport.scope["session_id"] = "0" * 32
        elif mutation == "expired":
            prior = mesh._grants[grant.token]
            mesh._grants[grant.token] = (prior[0], prior[1], time.monotonic() - 1)
        elif mutation == "forged":
            grant = SynCogGrant(grant.token)
        elif mutation == "missing":
            grant = None
        with pytest.raises(ScopeDeniedError):
            mesh.consume(grant, op, source, context, actor, agent)
        assert not mesh._grants or mutation == "missing"


def test_grant_cannot_be_replayed_and_node_loss_fences_consumption():
    transport = PublicTransportDouble()
    mesh = ReleasedSynWorkerNodeMesh(transport)
    with mesh.running():
        args = inputs(transport)
        grant = mesh.authorize(*args)
        mesh.consume(grant, *args)
        with pytest.raises(ScopeDeniedError):
            mesh.consume(grant, *args)
        grant = mesh.authorize(*args)
        transport.live = False
        with pytest.raises(StateMismatchError):
            mesh.consume(grant, *args)


def test_default_executor_rejects_legacy_mesh_and_missing_native_grant():
    executor = SandboxedCogExecutor.__new__(SandboxedCogExecutor)
    executor.syn_mesh = SynWorkerNodeMesh()
    transport = PublicTransportDouble()
    args = inputs(transport)
    with pytest.raises(CapabilityUnavailableError):
        asyncio.run(executor.execute(*args))
    executor.syn_mesh = ReleasedSynWorkerNodeMesh(transport)
    with executor.syn_mesh.running(), pytest.raises(ScopeDeniedError, match="absent"):
        asyncio.run(executor.execute(*args))


def test_heartbeat_receives_only_from_bound_native_agent_and_loss_faults_workers():
    async def run(mesh, transport):
        first, second = transport.agents
        async with mesh.node(transport.workspace_id, first, uuid4()) as left:
            async with mesh.node(transport.workspace_id, second, uuid4()) as right:
                await asyncio.to_thread(left.pulse)
                deadline = time.monotonic() + 3
                while not right.snapshot()["peers"]:
                    assert time.monotonic() < deadline
                    await asyncio.sleep(0.02)
                assert right.snapshot()["peer_heartbeats_received"] >= 1
                # A correctly decoded message may still lie about logical Agent identity.
                payload = next(iter(right._peers.values()))[0].to_dict()
                payload["agent_id"] = str(second)
                forged = {
                    "envelope": {
                        "header": {
                            "topic": HEARTBEAT_TYPE,
                            "node": transport.participant(first).selected_hash,
                            "boot_id": "b" * 32,
                            "workspace_id": str(transport.workspace_id),
                        }
                    },
                    "payload": payload,
                }
                transport.queues[first].put(forged)
                while not left.fault_event.is_set():
                    assert time.monotonic() < deadline
                    await asyncio.sleep(0.02)
                assert right.fault_event.is_set()
                with pytest.raises(StateMismatchError):
                    mesh.probe(transport.workspace_id, first)

    transport = PublicTransportDouble()
    mesh = ReleasedSynWorkerNodeMesh(transport, interval_seconds=1)
    with mesh.running():
        asyncio.run(run(mesh, transport))
