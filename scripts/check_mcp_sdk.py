#!/usr/bin/env python3
"""Run Potbot's bounded HTTP client against the pinned upstream MCP SDK in process."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from importlib.metadata import version
from typing import Annotated, Any

import httpx
from mcp.server import MCPServer, Server
from mcp.types import (
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
    ListToolsResult,
    Tool,
)
from pydantic import Field
from starlette.applications import Starlette

from potbot.domain.errors import ScopeDeniedError
from potbot.tools.mcp_confirmation import McpConfirmation, McpConfirmationPrompt
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


async def _verify_header_parameter() -> None:
    server = MCPServer("Potbot header conformance fixture")

    @server.tool()
    def lookup(region: Annotated[str, Field(json_schema_extra={"x-mcp-header": "Region"})]) -> str:
        return region

    app = server.streamable_http_app(json_response=True)
    observed: list[str | None] = []

    async def observe(request: httpx.Request) -> None:
        if request.headers.get("Mcp-Method") == "tools/call":
            observed.append(request.headers.get("Mcp-Param-Region"))

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            event_hooks={"request": [observe]},
        ) as http:
            inspect = McpHttpTransport(
                ENDPOINT,
                http,
                network_approval=lambda _: None,
                allow_test_loopback=True,
            )
            discovery = McpClient(inspect.exchange, http_transport=inspect)
            approval = (await discovery.refresh_tools()).tool("lookup")
            transport = McpHttpTransport(
                ENDPOINT,
                http,
                network_approval=lambda _: None,
                allow_test_loopback=True,
                header_tool=approval,
            )

            def admit(tool: McpToolInfo, arguments: Mapping[str, Any]) -> None:
                if tool != approval or arguments != {"region": " 東京 "}:
                    raise ScopeDeniedError("Unexpected SDK routing fixture dispatch")

            client = McpClient(transport.exchange, admit=admit, http_transport=transport)
            catalog = await client.refresh_tools()
            assert client.protocol_version == MODERN
            reply = await client.call_tool(
                "lookup",
                {"region": " 東京 "},
                pinned_catalog_hash=catalog.fingerprint,
                pinned_tool_hash=approval.fingerprint,
            )
            assert not reply.is_error
            assert observed == ["=?base64?IOadseS6rCA=?="]


async def _verify_form_confirmation() -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    }
    question = ElicitRequest(
        params=ElicitRequestFormParams(
            message="Show the computed sum?",
            requested_schema={
                "type": "object",
                "properties": {"confirmed": {"type": "boolean"}},
                "required": ["confirmed"],
            },
        )
    )
    rounds: list[int] = []

    async def list_tools(_ctx, _params):
        return ListToolsResult(
            tools=[
                Tool(name="sum_after_confirmation", description="Add numbers", input_schema=schema)
            ]
        )

    async def call_tool(ctx, params):
        rounds.append(ctx.request_id)
        assert params.arguments == {"a": 19, "b": 23}
        if not params.input_responses:
            return InputRequiredResult(
                input_requests={"verify": question}, request_state="opaque-v1"
            )
        assert params.request_state == "opaque-v1"
        answer = params.input_responses["verify"]
        assert isinstance(answer, ElicitResult)
        assert answer.action == "accept" and answer.content == {"confirmed": True}
        return CallToolResult(content=[], structured_content={"sum": 42})

    server = Server("Potbot confirmation fixture", on_list_tools=list_tools, on_call_tool=call_tool)
    app = server.streamable_http_app(json_response=True)

    async def approve(prompt: McpConfirmationPrompt) -> bool:
        assert prompt == McpConfirmationPrompt(
            "sum_after_confirmation", "confirmed", "Show the computed sum?"
        )
        return True

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
            transport = McpHttpTransport(
                ENDPOINT,
                http,
                network_approval=lambda _: None,
                allow_test_loopback=True,
            )
            client = McpClient(
                transport.exchange,
                admit=lambda *_: None,
                http_transport=transport,
            )
            catalog = await client.refresh_tools()
            tool = catalog.tool("sum_after_confirmation")
            confirmation = McpConfirmation(
                tool.fingerprint,
                tool.to_capability(server_slug="fixture", effect_class="read_only"),
                "confirmed",
                approve,
            )
            reply = await client.call_tool(
                tool.name,
                {"a": 19, "b": 23},
                pinned_catalog_hash=catalog.fingerprint,
                pinned_tool_hash=tool.fingerprint,
                confirmation=confirmation,
            )
    assert not reply.is_error and reply.structured_content == {"sum": 42}
    assert len(rounds) == 2 and rounds[0] != rounds[1]


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
    await _verify_header_parameter()
    await _verify_form_confirmation()
    print(
        json.dumps(
            {
                "sdk_version": installed,
                "passed": results,
                "header_parameter": True,
                "form_confirmation": True,
            },
            sort_keys=True,
        )
    )


async def main() -> None:
    async with asyncio.timeout(30):
        await check()


if __name__ == "__main__":
    logging.disable(logging.INFO)
    asyncio.run(main())
