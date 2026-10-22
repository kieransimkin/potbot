"""T02.2: Streamable HTTP isolation and dual-era contract tests using mock IO."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    ScopeDeniedError,
)

from potbot.tools.mcp_http import McpHttpTransport
from potbot.tools.mcp_protocol import McpClient, McpProtocolError

URL = "https://mcp.fixture.example/mcp"


def run(coro):
    return asyncio.run(coro)


def listed_tool(name="add"):
    return {
        "name": name,
        "description": "Add numbers",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
    }


def respond(request, result):
    req = json.loads(request.content)
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": req["id"], "result": result})


def modern_fixture(request):
    row = json.loads(request.content)
    if row["method"] == "server/discover":
        return respond(
            request,
            {
                "resultType": "complete",
                "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {}},
            },
        )
    if row["method"] == "tools/list":
        return respond(request, {"resultType": "complete", "tools": [listed_tool()]})
    assert row["method"] == "tools/call"
    return respond(
        request,
        {
            "resultType": "complete",
            "content": [{"type": "text", "text": "8"}],
            "structuredContent": {"sum": 8},
        },
    )


def test_modern_http_headers_and_trusted_token_supplier():
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url == URL
        assert request.headers["mcp-protocol-version"] == "2026-07-28"
        assert request.headers["mcp-method"] == json.loads(request.content)["method"]
        assert request.headers["authorization"] == "Bearer TEST_FIXTURE_TOKEN"
        assert request.headers["accept"] == "application/json, text/event-stream"
        assert request.headers.get("mcp-session-id") is None
        return modern_fixture(request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as h:
            trans = McpHttpTransport(
                URL,
                h,
                network_approval=lambda u: seen.append("approved"),
                bearer_supplier=lambda: "TEST_FIXTURE_TOKEN",
            )
            client = McpClient(trans.exchange, admit=lambda tool, args: None)
            cat = await client.refresh_tools()
            tool = cat.tool("add")
            result = await client.call_tool(
                "add",
                {"a": 5, "b": 3},
                pinned_catalog_hash=cat.fingerprint,
                pinned_tool_hash=tool.fingerprint,
            )
            assert result.structured_content == {"sum": 8}

    run(scenario())
    assert len([x for x in seen if isinstance(x, httpx.Request)]) == 3
    last = next(x for x in reversed(seen) if isinstance(x, httpx.Request))
    assert last.headers["mcp-name"] == "add"
    assert (
        json.loads(last.content)["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"]
        == "2026-07-28"
    )


def test_legacy_handshake_session_applies_only_to_legacy_calls():
    recorded = []

    def handler(request):
        row = json.loads(request.content)
        recorded.append((row["method"], dict(request.headers)))
        if row["method"] == "server/discover":
            return httpx.Response(
                404,
                json={
                    "jsonrpc": "2.0",
                    "id": row["id"],
                    "error": {"code": -32601, "message": "Method not found"},
                },
            )
        if row["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "SESSION-123"},
                json={
                    "jsonrpc": "2.0",
                    "id": row["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "serverInfo": {"name": "x", "version": "1"},
                    },
                },
            )
        if row["method"] == "notifications/initialized":
            return httpx.Response(202)
        if row["method"] == "tools/list":
            return respond(request, {"tools": [listed_tool()]})
        return respond(request, {"content": [{"type": "text", "text": "8"}]})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as h:
            trans = McpHttpTransport(URL, h, network_approval=lambda u: None)
            client = McpClient(trans.exchange, admit=lambda tool, args: None)
            cat = await client.refresh_tools()
            assert client.protocol_version == "2025-11-25"
            item = cat.tool("add")
            await client.call_tool(
                "add",
                {"a": 1, "b": 7},
                pinned_catalog_hash=cat.fingerprint,
                pinned_tool_hash=item.fingerprint,
            )

    run(scenario())
    assert [x[0] for x in recorded] == [
        "server/discover",
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    assert "mcp-session-id" not in recorded[1][1]
    assert recorded[-1][1]["mcp-session-id"] == "SESSION-123"
    assert "mcp-method" not in recorded[-1][1]


def test_bounded_sse_parses_notification_then_result():
    def handler(request):
        if json.loads(request.content)["method"] != "tools/call":
            return modern_fixture(request)
        ident = json.loads(request.content)["id"]
        sse = (
            "event: message\ndata: "
            + json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}}
            )
            + "\n\nevent: message\ndata: "
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": ident,
                    "result": {"content": [{"type": "text", "text": "8"}]},
                }
            )
            + "\n\n"
        )
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as h:
            client = McpClient(
                McpHttpTransport(URL, h, network_approval=lambda u: None).exchange,
                admit=lambda a, b: None,
            )
            cat = await client.refresh_tools()
            item = cat.tool("add")
            result = await client.call_tool(
                "add",
                {"a": 1, "b": 7},
                pinned_catalog_hash=cat.fingerprint,
                pinned_tool_hash=item.fingerprint,
            )
            assert result.content[0]["text"] == "8"

    run(scenario())


@pytest.mark.parametrize(
    "status,expected",
    [
        (302, ScopeDeniedError),
        (401, ScopeDeniedError),
        (403, ScopeDeniedError),
        (500, IndeterminateExecutionError),
        (429, IndeterminateExecutionError),
        (418, McpProtocolError),
    ],
)
def test_http_refuses_redirects_auth_failures_and_server_errors(status, expected):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(status, headers={"Location": "http://127.0.0.1/private"})
            )
        ) as h:
            client = McpClient(McpHttpTransport(URL, h, network_approval=lambda u: None).exchange)
            with pytest.raises(expected):
                await client.connect()

    run(scenario())


def test_http_no_network_authority_denies_before_request():
    counts = []

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: (counts.append(req), modern_fixture(req))[1])
        ) as h:
            trans = McpHttpTransport(URL, h)
            with pytest.raises(ScopeDeniedError):
                await McpClient(trans.exchange).connect()

    run(scenario())
    assert counts == []


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example/mcp",
        "https://127.0.0.1/mcp",
        "https://[::1]/mcp",
        "https://user:pass@evil.example/mcp",
        "https://evil.example/mcp?key=secret",
        "https://evil.example/mcp#fragment",
        "ftp://evil.example/mcp",
    ],
)
def test_bad_endpoint_rejected(url):
    with pytest.raises((InvalidInputError, ScopeDeniedError)):
        McpHttpTransport(url, object(), network_approval=lambda u: None)


def test_sse_disconnection_indeterminate_not_success():
    def handler(request):
        if json.loads(request.content)["method"] != "tools/call":
            return modern_fixture(request)
        return httpx.Response(
            200,
            content="event: message\ndata: "
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/progress"})
            + "\n\n",
            headers={"Content-Type": "text/event-stream"},
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as h:
            client = McpClient(
                McpHttpTransport(URL, h, network_approval=lambda u: None).exchange,
                admit=lambda a, b: None,
            )
            cat = await client.refresh_tools()
            with pytest.raises(IndeterminateExecutionError):
                await client.call_tool(
                    "add",
                    {"a": 1, "b": 7},
                    pinned_catalog_hash=cat.fingerprint,
                    pinned_tool_hash=cat.tool("add").fingerprint,
                )

    run(scenario())


def test_http_body_size_and_invalid_schema_result_rejected():
    def handler(request):
        if json.loads(request.content)["method"] != "tools/call":
            return modern_fixture(request)
        return respond(request, {"content": [{"type": "text", "text": "x" * (2_100_000)}]})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as h:
            client = McpClient(
                McpHttpTransport(URL, h, network_approval=lambda u: None).exchange,
                admit=lambda a, b: None,
            )
            cat = await client.refresh_tools()
            with pytest.raises(McpProtocolError):
                await client.call_tool(
                    "add",
                    {"a": 1, "b": 7},
                    pinned_catalog_hash=cat.fingerprint,
                    pinned_tool_hash=cat.tool("add").fingerprint,
                )

    run(scenario())


def test_no_secret_in_errors_or_redirect_and_no_retries():
    attempts = []

    def handler(req):
        attempts.append(dict(req.headers))
        return httpx.Response(302, headers={"Location": "https://attacker.example/mcp"})

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as h:
            client = McpClient(
                McpHttpTransport(
                    URL,
                    h,
                    network_approval=lambda u: None,
                    bearer_supplier=lambda: "SECRET_FOR_TEST_ONLY",
                ).exchange
            )
            with pytest.raises(ScopeDeniedError) as exc:
                await client.connect()
            assert "SECRET_FOR_TEST_ONLY" not in str(exc.value)

    run(scenario())
    assert len(attempts) == 1
