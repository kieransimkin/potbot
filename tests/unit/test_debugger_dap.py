"""Wire conformance and hostile stream tests; these do not qualify a real debugger."""

import asyncio
import json

import pytest

from potbot.debugging.dap import DapAdapter, _frame
from potbot.domain.errors import IndeterminateExecutionError, ToolFailureError


class Writer:
    def __init__(self):
        self.messages = []
        self.closed = False

    def write(self, data):
        self.messages.append(json.loads(data.split(b"\r\n\r\n", 1)[1]))

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def respond(reader, request, *, body=None, success=True, sequence=1):
    reader.feed_data(
        _frame(
            {
                "seq": sequence,
                "type": "response",
                "request_seq": request["seq"],
                "command": request["command"],
                "success": success,
                "body": body or {},
            }
        )
    )


def test_fragmented_utf8_frames_and_out_of_order_response_correlation():
    async def scenario():
        reader, writer = asyncio.StreamReader(), Writer()
        adapter = DapAdapter(reader, writer)
        one = await adapter.begin("threads", {})
        two = await adapter.begin("threads", {})
        respond(reader, writer.messages[1], body={"threads": [{"id": 1, "name": "二"}]})
        raw = _frame(
            {
                "seq": 2,
                "type": "response",
                "request_seq": one,
                "command": "threads",
                "success": True,
                "body": {"threads": [{"id": 2, "name": "α"}]},
            }
        )
        for byte in raw:
            reader.feed_data(bytes([byte]))
        assert (await adapter.finish(two))["threads"][0]["name"] == "二"
        assert (await adapter.finish(one))["threads"][0]["name"] == "α"
        await adapter.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "raw",
    [
        b"Content-Length: 999999999\r\n\r\n",
        b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
        b"Content-Length: 3\r\n\r\nxxx",
        _frame(
            {
                "seq": 1,
                "type": "response",
                "request_seq": 999,
                "command": "threads",
                "success": True,
            }
        ),
    ],
)
def test_bad_frames_and_unknown_responses_poison_stream(raw):
    async def scenario():
        reader, writer = asyncio.StreamReader(), Writer()
        adapter = DapAdapter(reader, writer)
        request = await adapter.begin("threads", {})
        reader.feed_data(raw)
        with pytest.raises(IndeterminateExecutionError):
            await adapter.finish(request)
        with pytest.raises(IndeterminateExecutionError):
            await adapter.begin("threads", {})
        assert writer.closed
        await adapter.close()

    asyncio.run(scenario())


def test_timeout_does_not_repeat_request_and_reverse_launch_is_rejected():
    async def scenario():
        reader, writer = asyncio.StreamReader(), Writer()
        adapter = DapAdapter(reader, writer, timeout_s=0.02)
        request = await adapter.begin("threads", {})
        reader.feed_data(
            _frame(
                {
                    "seq": 20,
                    "type": "request",
                    "command": "runInTerminal",
                    "arguments": {"args": ["malicious"]},
                }
            )
        )
        with pytest.raises(IndeterminateExecutionError):
            await adapter.finish(request)
        assert [m["command"] for m in writer.messages] == ["threads", "runInTerminal"]
        assert writer.messages[1]["success"] is False
        await adapter.close()

    asyncio.run(scenario())


def test_remote_error_details_are_not_exposed():
    async def scenario():
        reader, writer = asyncio.StreamReader(), Writer()
        adapter = DapAdapter(reader, writer)
        request = await adapter.begin("threads", {})
        respond(reader, writer.messages[0], body={"private": "secret-value"}, success=False)
        with pytest.raises(ToolFailureError) as caught:
            await adapter.finish(request)
        assert "secret-value" not in str(caught.value)
        await adapter.close()

    asyncio.run(scenario())
