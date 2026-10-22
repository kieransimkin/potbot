"""T02.1: real subprocess fixture speaks modern/legacy JSON-RPC over pipes."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from potbot.domain.errors import CapabilityUnavailableError, IndeterminateExecutionError, InvalidInputError, ScopeDeniedError
from potbot.tools.mcp_protocol import McpClient, McpProtocolError
from potbot.tools.mcp_stdio import McpStdioTransport

SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_stdio_server.py"


async def serve(*, era="modern", scenario="normal", timeout=2.0, allow=True):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", str(SERVER), "--era", era, "--scenario", scenario,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=3 * 1024 * 1024,
    )
    assert proc.stdin is not None and proc.stdout is not None
    transport = McpStdioTransport(proc.stdout, proc.stdin, request_timeout_s=timeout)
    def admit(tool, arguments):
        if not allow:
            raise ScopeDeniedError("No write grant")
    client = McpClient(transport.exchange, admit=admit if allow is not None else None)
    try:
        await client.connect()
        cat = await client.refresh_tools()
        yield client, cat
    finally:
        await transport.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


def run(coro):
    return asyncio.run(coro)


def test_modern_stdio_real_child_executes_pinned_tool():
    async def main():
        async for client, cat in serve():
            item = cat.tool("add")
            result = await client.call_tool("add", {"a": 3, "b": 4},
                pinned_catalog_hash=cat.fingerprint, pinned_tool_hash=item.fingerprint)
            assert result.structured_content == {"sum": 7}
    run(main())


def test_legacy_stdio_handshake_executes():
    async def main():
        async for client, cat in serve(era="legacy"):
            assert client.protocol_version == "2025-11-25"
            item = cat.tool("add")
            result = await client.call_tool("add", {"a": 7, "b": 5},
                pinned_catalog_hash=cat.fingerprint, pinned_tool_hash=item.fingerprint)
            assert result.structured_content == {"sum": 12}
    run(main())


def test_paginated_discovery():
    async def main():
        async for client, cat in serve(scenario="pages"):
            assert [t.name for t in cat.tools] == ["add", "multiply"]
    run(main())


@pytest.mark.parametrize("scenario", ["cursor_cycle", "duplicates", "bad_schema"])
def test_bad_catalog_rejected(scenario):
    async def main():
        with pytest.raises(McpProtocolError):
            async for client, cat in serve(scenario=scenario):
                pass
    run(main())


@pytest.mark.parametrize("scenario,error", [
    ("crash", IndeterminateExecutionError),
    ("timeout", IndeterminateExecutionError),
    ("wrong_id", McpProtocolError),
    ("oversize", McpProtocolError),
    ("input_required", CapabilityUnavailableError),
])
def test_child_failure_modes_are_explicit(scenario, error):
    async def main():
        async for client, cat in serve(scenario=scenario, timeout=2.0):
            item = cat.tool("add")
            if scenario == "timeout":
                # Only the call is short; subprocess startup and discovery are not.
                client._exchange.__self__.timeout = 0.2
            with pytest.raises(error):
                await client.call_tool("add", {"a": 1, "b": 2},
                    pinned_catalog_hash=cat.fingerprint, pinned_tool_hash=item.fingerprint)
    run(main())


def test_server_notifications_and_server_requests_do_not_grant_capabilities():
    async def main():
        for scenario in ("notify", "server_request"):
            async for client, cat in serve(scenario=scenario):
                item = cat.tool("add")
                result = await client.call_tool("add", {"a": 1, "b": 2},
                    pinned_catalog_hash=cat.fingerprint, pinned_tool_hash=item.fingerprint)
                assert result.structured_content == {"sum": 3}
    run(main())


def test_tool_errors_are_results_not_protocol_success_claims():
    async def main():
        async for client, cat in serve(scenario="tool_error"):
            item = cat.tool("add")
            result = await client.call_tool("add", {"a": 1, "b": 2},
                pinned_catalog_hash=cat.fingerprint, pinned_tool_hash=item.fingerprint)
            assert result.is_error is True
    run(main())


def test_missing_admission_denies_before_io():
    async def main():
        async for client, cat in serve(allow=None):
            item = cat.tool("add")
            with pytest.raises(ScopeDeniedError):
                await client.call_tool("add", {"a": 1, "b": 2},
                    pinned_catalog_hash=cat.fingerprint, pinned_tool_hash=item.fingerprint)
    run(main())


def test_invalid_arguments_rejected_before_io():
    async def main():
        async for client, cat in serve():
            item = cat.tool("add")
            with pytest.raises(InvalidInputError):
                await client.call_tool("add", {"a": "bad", "b": 2},
                    pinned_catalog_hash=cat.fingerprint, pinned_tool_hash=item.fingerprint)
    run(main())


def test_failed_stream_not_reused_for_second_side_effect():
    async def main():
        async for client, cat in serve(scenario="wrong_id"):
            item = cat.tool("add")
            with pytest.raises(McpProtocolError):
                await client.call_tool("add", {"a": 1, "b": 2},
                    pinned_catalog_hash=cat.fingerprint, pinned_tool_hash=item.fingerprint)
            with pytest.raises(IndeterminateExecutionError):
                await client.call_tool("add", {"a": 1, "b": 2},
                    pinned_catalog_hash=cat.fingerprint, pinned_tool_hash=item.fingerprint)
    run(main())


def test_cancelled_stdio_write_poisons_stream_instead_of_replaying():
    async def main():
        async for client, cat in serve(scenario="timeout"):
            tool = cat.tool("add")
            pending = asyncio.create_task(client.call_tool(
                "add", {"a": 1, "b": 2},
                pinned_catalog_hash=cat.fingerprint, pinned_tool_hash=tool.fingerprint))
            await asyncio.sleep(0.05)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            with pytest.raises(IndeterminateExecutionError):
                await client.call_tool("add", {"a": 1, "b": 2},
                    pinned_catalog_hash=cat.fingerprint, pinned_tool_hash=tool.fingerprint)
    run(main())
