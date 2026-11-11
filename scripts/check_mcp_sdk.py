#!/usr/bin/env python3
"""Run Potbot's bounded HTTP client against the pinned upstream MCP SDK in process."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from importlib.metadata import version
from typing import Any

import httpx
from mcp.server import MCPServer
from starlette.applications import Starlette

from potbot.domain.errors import ScopeDeniedError
from potbot.tools.mcp_http import McpHttpTransport
from potbot.tools.mcp_protocol import LEGACY, MODERN, McpClient, McpToolInfo

SDK_VERSION = "2.3.0"
ENDPOINT = "http://127.0.0.1:8000/mcp"


async def _verify_protocol(app: Starlette, protocol: str) -> None:
    received: list[tuple[str, bool]] = []

    async def observe(request: httpx.Request) -> None:
        body = json.loads(request.content)
        received.append((body["method"], "mcp-session-id" in request.headers))

    def admit(tool: McpToolInfo, arguments: Mapping[str, Any]) -> None:
        if tool.name != "add" or arguments != {"a": 2, "b": 3}:
            raise ScopeDeniedError("Unexpected SDK fixture dispatch")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        event_hooks={"request": [observe]},
        trust_env=False,
    ) as client:
        transport = McpHttpTransport(
            ENDPOINT, client, network_approval=lambda _: None, allow_test_loopback=True
        )

        async def exchange(message: dict[str, Any], expect_reply: bool) -> dict[str, Any] | None:
            # Select the SDK's real legacy path by simulating the
            # pre-discovery boundary before forwarding legacy frames.
            if protocol == LEGACY and message["method"] == "server/discover":
                return {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "Legacy fixture"},
                }
            return await transport.exchange(message, expect_reply)

        mcp = McpClient(exchange, admit=admit)
        catalog = await mcp.refresh_tools()
        tool = catalog.tool("add")
        result = await mcp.call_tool(
            "add",
            {"a": 2, "b": 3},
            pinned_catalog_hash=catalog.fingerprint,
            pinned_tool_hash=tool.fingerprint,
        )
        assert mcp.protocol_version == protocol
        assert not result.is_error and result.structured_content == {"sum": 5}
        if protocol == MODERN:
            assert received == [
                ("server/discover", False),
                ("tools/list", False),
                ("tools/call", False),
            ]
        else:
            assert received == [
                ("initialize", False),
                ("notifications/initialized", True),
                ("tools/list", True),
                ("tools/call", True),
            ]


async def check() -> None:
    installed = version("mcp")
    if installed != SDK_VERSION:
        raise SystemExit(f"Expected official MCP SDK {SDK_VERSION}, found {installed}")
    server = MCPServer("Potbot conformance fixture")

    @server.tool()
    def add(a: int, b: int) -> dict[str, int]:
        return {"sum": a + b}

    results = []
    for json_response in (True, False):
        app = server.streamable_http_app(json_response=json_response)
        async with app.router.lifespan_context(app):
            for protocol in (MODERN, LEGACY):
                await _verify_protocol(app, protocol)
                results.append(
                    {"protocol": protocol, "response": "json" if json_response else "sse"}
                )
    print(json.dumps({"sdk_version": installed, "passed": results}, sort_keys=True))


async def main() -> None:
    async with asyncio.timeout(30):
        await check()


if __name__ == "__main__":
    logging.disable(logging.INFO)
    asyncio.run(main())
