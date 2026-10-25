"""SYN worker-node protocol, operator-scoped delivery and failure safety."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.tools.syn_worker_nodes import (
    HEARTBEAT_TYPE, MAX_WORKSPACE_NODES, SynWorkerNodeMesh, SynHeartbeat,
)


def test_two_worker_instances_exchange_real_heartbeat_messages() -> None:
    async def exercise():
        workspace, a, b = uuid4(), uuid4(), uuid4()
        mesh = SynWorkerNodeMesh(interval_seconds=0.05)
        async with mesh.node(workspace, a, uuid4()) as first:
            async with mesh.node(workspace, b, uuid4()) as second:
                await asyncio.sleep(0.22)
                view = mesh.status(workspace)
                assert view["node_count"] == 2
                assert view["nodes"][0]["node_id"] != view["nodes"][1]["node_id"]
                assert first._received >= 2 and second._received >= 2
                assert first._sent >= 3 and second._sent >= 3
                assert first.healthy and second.healthy
                assert view["nodes"][0]["peers"][0]["state"] == "live"
                assert str(workspace) not in str(view.get("private_paths", ""))
        assert mesh.status(workspace)["nodes"] == []
    asyncio.run(exercise())


def test_cross_workspace_heartbeats_do_not_cross() -> None:
    async def exercise():
        mesh = SynWorkerNodeMesh(interval_seconds=0.05)
        a, b = uuid4(), uuid4()
        async with mesh.node(a, uuid4(), uuid4()) as first:
            async with mesh.node(b, uuid4(), uuid4()) as second:
                await asyncio.sleep(0.16)
                assert first._received == 0 and second._received == 0
                forged = SynHeartbeat(b, second.agent_id, second.worker_run_id,
                                      second.node_id, second.incarnation, 100, 1000, "admitted")
                with pytest.raises(ScopeDeniedError):
                    first._receive(forged)
                with pytest.raises(ScopeDeniedError):
                    mesh._broadcast(first, forged)
    asyncio.run(exercise())


def test_peer_duplicate_replay_and_identity_spoof_are_rejected() -> None:
    async def exercise():
        mesh = SynWorkerNodeMesh(interval_seconds=0.3)
        w = uuid4()
        async with mesh.node(w, uuid4(), uuid4()) as first:
            async with mesh.node(w, uuid4(), uuid4()) as second:
                first.pulse()
                beat, _ = second._peers[first.node_id]
                assert beat.to_dict()["type_ref"] == HEARTBEAT_TYPE
                with pytest.raises(StateMismatchError, match="replay"):
                    second._receive(beat)
                with pytest.raises(StateMismatchError, match="incarnation"):
                    second._receive(replace(beat, incarnation=uuid4(), sequence=beat.sequence + 1))
                with pytest.raises(ScopeDeniedError, match="spoofed"):
                    mesh._broadcast(first, replace(beat, agent_id=uuid4(), sequence=beat.sequence + 1))
    asyncio.run(exercise())


def test_worker_instance_is_one_use_and_registration_is_bounded() -> None:
    async def exercise():
        mesh = SynWorkerNodeMesh(interval_seconds=0.5)
        w = uuid4()
        nodes = [mesh.node(w, uuid4(), uuid4()) for _ in range(MAX_WORKSPACE_NODES)]
        try:
            for node in nodes:
                await node.__aenter__()
            with pytest.raises(ScopeDeniedError, match="capacity"):
                await mesh.node(w, uuid4(), uuid4()).__aenter__()
        finally:
            for node in reversed(nodes):
                await node.__aexit__(None, None, None)
        with pytest.raises(StateMismatchError, match="one-use"):
            await nodes[0].__aenter__()
    asyncio.run(exercise())


def test_silent_peer_becomes_suspect_without_becoming_authoritative_dead() -> None:
    async def exercise():
        now = [50.0]
        mesh = SynWorkerNodeMesh(interval_seconds=0.1, clock=lambda: now[0])
        w = uuid4()
        async with mesh.node(w, uuid4(), uuid4()) as first:
            async with mesh.node(w, uuid4(), uuid4()) as second:
                first.pulse()
                now[0] += 0.5
                peer = second.snapshot()["peers"][0]
                assert peer["state"] == "suspect"
                assert second.healthy
    asyncio.run(exercise())


def test_fault_in_heartbeat_loop_fences_worker_control() -> None:
    async def exercise():
        mesh = SynWorkerNodeMesh(interval_seconds=0.05)
        node = mesh.node(uuid4(), uuid4(), uuid4())
        async with node:
            original = mesh._broadcast
            def broken(*_args):
                raise OSError("operator relay broken")
            mesh._broadcast = broken
            await asyncio.sleep(0.13)
            assert not node.healthy and node.fault_event.is_set()
            mesh._broadcast = original
    asyncio.run(exercise())
