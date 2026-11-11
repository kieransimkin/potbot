"""Admission and reconciliation for the host-only Syn Context route."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.context.rpc_contract import SCHEMA
from potbot.context.rpc_transport import response_bytes
from potbot.context.syn_context_route import METHOD, SynContextRoute, WorkerRpcBinding
from potbot.context.worker_channel import ContextWorkerChannel
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import IndeterminateExecutionError, ScopeDeniedError

BOOT = "b" * 32


def binding():
    return WorkerRpcBinding(uuid4(), uuid4(), uuid4(), "a" * 64, "e" * 32, "c" * 64, "lease", 1)


def message():
    return {
        "schema": SCHEMA,
        "request_id": str(uuid4()),
        "method": "context.read",
        "scope": "agent",
        "role": "MODEL.md",
    }


def header(workspace, transport_id, size):
    return {
        "profile": "syn.local-rpc-dispatch/1",
        "workspace_id": str(workspace),
        "method": METHOD,
        "request_id": str(transport_id),
        "size": size,
        "boot_id": BOOT,
        "caller_uid": 1000,
    }


def test_only_matching_dispatch_can_reach_the_bound_agent():
    first = binding()
    second = replace(first, agent_id=uuid4(), invocation_id=uuid4(), agent_key_sha256="f" * 64)
    expected = [first, second]
    seen = []
    dispatches = []

    def round_trip(transport_id, payload):
        dispatches.append(transport_id)
        selected = header(first.workspace_id, transport_id, len(payload))
        for change in (
            {"workspace_id": str(uuid4())},
            {"boot_id": "0" * 32},
            {"caller_uid": 1001},
            {"agent_id": str(second.agent_id)},
            {"request_id": str(uuid4())},
        ):
            with pytest.raises(ScopeDeniedError):
                route.accept(selected | change, payload)
        with pytest.raises(ScopeDeniedError):
            route.accept(selected, bytes([payload[0] ^ 1]) + payload[1:])
        reply = route.accept(selected, payload)
        with pytest.raises(ScopeDeniedError):
            route.accept(selected, payload)
        return reply, BOOT

    route = SynContextRoute(
        workspace_id=first.workspace_id, operator_boot=BOOT, caller_uid=1000, round_trip=round_trip
    )
    for identity in expected:

        def handler(request, agent=identity.agent_id):
            seen.append((agent, request.request_id))
            return {"result": "private"}

        with pytest.raises(ScopeDeniedError):
            route.bind(replace(identity, workspace_id=uuid4()), lambda _: None, handler)

        request = message()
        lease = route.bind(identity, lambda _: None, handler)
        reply = json.loads(lease.call(json.dumps(request).encode()))
        assert reply["request_id"] == request["request_id"]
        assert str(dispatches[-1]) != request["request_id"]
        assert reply["result"] == {"result": "private"}
        lease.close()
        with pytest.raises(ScopeDeniedError):
            lease.call(b"{}")
    assert [agent for agent, _ in seen] == [first.agent_id, second.agent_id]
    assert len(set(dispatches)) == 4


def test_fenced_delivery_does_not_reach_handler():
    worker = binding()
    live = True

    def check(selected):
        assert selected == worker
        if not live:
            raise ScopeDeniedError("lease fenced")

    def round_trip(transport_id, payload):
        nonlocal live
        live = False
        return route.accept(header(worker.workspace_id, transport_id, len(payload)), payload), BOOT

    route = SynContextRoute(
        workspace_id=worker.workspace_id, operator_boot=BOOT, caller_uid=1000, round_trip=round_trip
    )
    invocation = route.bind(worker, check, lambda _: pytest.fail("Fenced request reached service"))
    try:
        with pytest.raises(IndeterminateExecutionError, match="original request ID"):
            invocation.call(json.dumps(message()).encode())
    finally:
        invocation.close()


def test_closing_an_invocation_waits_for_its_inflight_effect():
    worker = binding()
    entered, finish, settled, closing = (threading.Event() for _ in range(4))
    applied = []
    dispatched = []

    def execute(request):
        entered.set()
        assert finish.wait(5)
        applied.append(request.request_id)
        return {}

    with ThreadPoolExecutor(max_workers=2) as threads:

        def round_trip(transport_id, payload):
            dispatched.append(transport_id)
            reply = route.accept(header(worker.workspace_id, transport_id, len(payload)), payload)
            assert not entered.is_set()
            return reply, BOOT

        route = SynContextRoute(
            workspace_id=worker.workspace_id,
            operator_boot=BOOT,
            caller_uid=1000,
            round_trip=round_trip,
        )
        invocation = route.bind(worker, lambda _: None, execute)
        request = message()

        def revoke():
            closing.set()
            invocation.close()
            settled.set()

        try:
            call = threads.submit(invocation.call, json.dumps(request).encode())
            assert entered.wait(5)
            closer = threads.submit(revoke)
            assert closing.wait(5)
            assert not settled.wait(0.05)
            with pytest.raises(ScopeDeniedError):
                invocation.call(json.dumps(request).encode())
        finally:
            finish.set()
        closer.result(5)
        with pytest.raises(IndeterminateExecutionError):
            call.result(5)
        assert settled.is_set()
    assert [str(x) for x in applied] == [request["request_id"]]
    assert len(dispatched) == 1


@pytest.mark.parametrize("lost_exchange", [1, 2])
def test_lost_signed_delivery_or_result_does_not_retry(lost_exchange):
    worker = binding()
    request = message()
    dispatched = []
    effects = []

    def round_trip(transport_id, payload):
        dispatched.append(transport_id)
        reply = route.accept(header(worker.workspace_id, transport_id, len(payload)), payload)
        if len(dispatched) == lost_exchange:
            raise TimeoutError("reply lost")
        return reply, BOOT

    def execute(operation):
        effects.append(str(operation.request_id))
        return {"applied": True}

    route = SynContextRoute(
        workspace_id=worker.workspace_id, operator_boot=BOOT, caller_uid=1000, round_trip=round_trip
    )
    invocation = route.bind(worker, lambda _: None, execute)
    try:
        with pytest.raises(IndeterminateExecutionError, match="original request ID"):
            invocation.call(json.dumps(request).encode())
        with pytest.raises(ScopeDeniedError):
            invocation.call(json.dumps(request).encode())
    finally:
        invocation.close()
    assert len(set(dispatched)) == len(dispatched) == lost_exchange
    assert effects == ([request["request_id"]] if lost_exchange == 2 else [])


@pytest.mark.parametrize("fence", [None, "agent", "lease"])
def test_worker_channel_uses_syn_route_and_rechecks_native_pins(fence, tmp_path, monkeypatch):
    worker = binding()
    principal = WorkspaceContext(worker.workspace_id, uuid4())
    lifecycle = []
    lease = SimpleNamespace(lease_id=worker.lease_id, generation=worker.lease_generation)
    agent_pin = (worker.agent_key_sha256, worker.agent_boot_id)
    request = message()
    sent = []

    class Writer:
        def write(self, wire):
            sent.append(json.loads(wire[4:]))

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def close(*, force):
        lifecycle.append(("stop", force))
        return SimpleNamespace(processes_stopped=True)

    process = SimpleNamespace(
        writer=Writer(),
        close=close,
        remaining_seconds=lambda: 30,
        isolation_identity=worker.isolation_sha256,
    )

    def round_trip(transport_id, payload):
        nonlocal agent_pin
        if fence == "agent":
            agent_pin = (worker.agent_key_sha256, "0" * 32)
        elif fence == "lease":
            lease.generation += 1
        return route.accept(header(worker.workspace_id, transport_id, len(payload)), payload), BOOT

    route = SynContextRoute(
        workspace_id=worker.workspace_id, operator_boot=BOOT, caller_uid=1000, round_trip=round_trip
    )
    grant = SimpleNamespace(session_id=uuid4())

    def execute(selected, operation):
        assert selected is grant
        lifecycle.append(("execute", str(operation.request_id)))
        return {"ok": "observed"}

    store = SimpleNamespace(
        ensure_agent_context=lambda *_: None,
        issue=lambda *args: (
            grant
            if args == (principal, worker.agent_id, worker.invocation_id)
            else pytest.fail("Wrong invocation issued")
        ),
        execute=execute,
        revoke=lambda _: lifecycle.append(("revoke", None)),
    )
    channel = ContextWorkerChannel(
        store=store,
        principal=principal,
        agent_id=worker.agent_id,
        guard=SimpleNamespace(lease=lease),
        registry=None,
        check_operator=lambda: None,
        syn_route=route,
        syn_agent=lambda: agent_pin,
    )
    monkeypatch.setattr(channel, "_validate_process", lambda _: worker.invocation_id)
    monkeypatch.setattr(channel, "_authorize", lambda: None)

    async def run():
        process.reader = asyncio.StreamReader()
        process.reader.feed_data(response_bytes(request))
        process.reader.feed_eof()
        return await channel.serve(process)

    if fence is None:
        result = asyncio.run(run())
        assert result.channel.requests == 1
        assert sent[0]["request_id"] == request["request_id"]
        assert sent[0]["result"] == {"ok": "observed"}
        assert lifecycle == [("execute", request["request_id"]), ("stop", False), ("revoke", None)]
    else:
        with pytest.raises(IndeterminateExecutionError):
            asyncio.run(run())
        assert not sent
        assert lifecycle == [("stop", True), ("revoke", None)]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux peer credentials required")
def test_context_destination_reads_one_typed_syn_dispatch():
    worker = binding()
    caller_uid = os.getuid()

    def round_trip(transport_id, payload):
        dispatch = header(worker.workspace_id, transport_id, len(payload))
        dispatch["caller_uid"] = caller_uid
        metadata = json.dumps(dispatch, sort_keys=True, separators=(",", ":")).encode()
        client, server = socket.socketpair()
        with client, server, ThreadPoolExecutor(max_workers=1) as threads:
            client.settimeout(3)
            with pytest.raises(ScopeDeniedError, match="peer"):
                route.serve_connection(server, operator_uid=caller_uid + 1)
            handling = threads.submit(route.serve_connection, server, operator_uid=caller_uid)
            client.sendall(struct.pack("!I", len(metadata)) + metadata + payload)
            with client.makefile("rb") as stream:
                (header_length,) = struct.unpack("!I", stream.read(4))
                reply_header = json.loads(stream.read(header_length))
                assert reply_header["profile"] == "syn.local-rpc-service-reply/1"
                assert reply_header["request_id"] == str(transport_id)
                reply = stream.read(reply_header["size"])
            handling.result(3)
        return reply, BOOT

    route = SynContextRoute(
        workspace_id=worker.workspace_id,
        operator_boot=BOOT,
        caller_uid=caller_uid,
        round_trip=round_trip,
    )
    invocation = route.bind(worker, lambda _: None, lambda _: {"proof": "socket"})
    request = message()
    try:
        reply = json.loads(invocation.call(json.dumps(request).encode()))
        assert reply["request_id"] == request["request_id"]
        assert reply["result"] == {"proof": "socket"}
    finally:
        invocation.close()
