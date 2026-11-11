"""Regression checks for the worker acknowledgement boundary around HTTP MCP."""

import asyncio

import pytest

from potbot.domain.errors import IndeterminateExecutionError
from potbot.domain.serialization import content_hash
from potbot.tools.mcp_http_worker import relay_http_call


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
