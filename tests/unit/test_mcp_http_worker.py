"""Regression checks for the worker acknowledgement boundary around HTTP MCP."""

import asyncio
import sys

import pytest

from potbot.domain.errors import IndeterminateExecutionError
from potbot.domain.serialization import content_hash
from potbot.tools.mcp_http_worker import CONFIRMATION_WORKER_ARGS, relay_http_call
from potbot.tools.mcp_stdio import McpStdioTransport


@pytest.mark.parametrize("lose", ["request", "receipt", None])
def test_http_relay_requires_worker_acknowledgement_and_never_replays(lose):
    async def exercise():
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 19, "b": 23}},
        }
        reply = {"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"sum": 42}}}
        calls = []

        async def worker(message, expect_reply):
            assert expect_reply
            if message["method"] == "tools/call":
                assert message == request
                digest = "0" * 64 if lose == "request" else content_hash(message)
            else:
                assert message["method"] == "potbot/receipt"
                assert message["params"] == {"sha256": content_hash(reply)}
                if lose == "receipt":
                    return None
                digest = message["params"]["sha256"]
            return {"jsonrpc": "2.0", "id": message["id"], "result": {"sha256": digest}}

        async def broker(message, expect_reply):
            assert message == request and expect_reply
            calls.append(message)
            return reply

        async def live():
            pass

        if lose:
            with pytest.raises(IndeterminateExecutionError):
                await relay_http_call(request, worker, broker, live)
        else:
            assert await relay_http_call(request, worker, broker, live) == reply
        assert calls == ([] if lose == "request" else [request])

    asyncio.run(exercise())


def test_http_relay_cancels_the_broker_when_the_worker_lease_is_lost():
    async def exercise():
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def worker(message, _):
            assert message == request
            return {"jsonrpc": "2.0", "id": 1, "result": {"sha256": content_hash(message)}}

        async def broker(*_):
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        async def live():
            if started.is_set():
                raise IndeterminateExecutionError("Worker is no longer admitted")

        with pytest.raises(IndeterminateExecutionError):
            await relay_http_call(request, worker, broker, live)
        assert stopped.is_set()

    asyncio.run(exercise())


@pytest.mark.parametrize("rounds", [1, 2])
def test_confirmation_worker_acknowledges_digests_and_exits_after_eof(rounds):
    async def exercise():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            *CONFIRMATION_WORKER_ARGS,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None and process.stdin is not None
        transport = McpStdioTransport(process.stdout, process.stdin, request_timeout_s=2)
        calls = []

        async def worker(message, expect_reply):
            if message["method"] == "tools/call":
                assert message["params"] == {"sha256": content_hash(request)}
            assert "private-state" not in repr(message)
            return await transport.exchange(message, expect_reply)

        async def broker(message, expect_reply):
            assert message == request and expect_reply
            calls.append(message["id"])
            return {"jsonrpc": "2.0", "id": message["id"], "result": {}}

        async def live():
            assert process.returncode is None

        try:
            for number in range(1, rounds + 1):
                request = {
                    "jsonrpc": "2.0",
                    "id": number,
                    "method": "tools/call",
                    "params": {"requestState": "private-state"} if number == 2 else {},
                }
                assert await relay_http_call(request, worker, broker, live, digest_only=True) == {
                    "jsonrpc": "2.0",
                    "id": number,
                    "result": {},
                }
            assert calls == list(range(1, rounds + 1))
        finally:
            await transport.close()
            assert await asyncio.wait_for(process.wait(), 3) == 0

    asyncio.run(exercise())
