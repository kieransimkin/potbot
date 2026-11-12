"""T02.2: Streamable HTTP isolation and dual-era contract tests using mock IO."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from potbot.domain.errors import (
    CapabilityUnavailableError,
    IndeterminateExecutionError,
    InvalidInputError,
    ScopeDeniedError,
)
from potbot.tools.mcp_confirmation import McpConfirmation, McpConfirmationPrompt
from potbot.tools.mcp_headers import InvalidMcpHeaderAnnotation, mirrored_headers
from potbot.tools.mcp_http import McpHttpTransport
from potbot.tools.mcp_protocol import (
    MODERN,
    TOOLS_CHANGED,
    TOOLS_LISTEN,
    McpClient,
    McpProtocolError,
    McpToolInfo,
)

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


def test_modern_http_mirrors_only_the_installed_tool_schema():
    definition = listed_tool("sum café")
    definition["inputSchema"] = {
        "type": "object",
        "properties": {
            "route": {
                "type": "object",
                "properties": {
                    "region": {"type": "string", "x-mcp-header": "Region"},
                    "active": {"type": "boolean", "x-mcp-header": "Active"},
                    "partition": {"type": "integer", "x-mcp-header": "Partition"},
                    "optional": {"type": ["string", "null"], "x-mcp-header": "Optional"},
                },
            }
        },
    }
    rejected = listed_tool("invalid-routing")
    rejected["inputSchema"] = {
        "type": "object",
        "properties": {
            "values": {
                "type": "array",
                "items": {"type": "string", "x-mcp-header": "Item"},
            },
        },
    }
    approved = McpToolInfo.from_wire(definition, allow_http_headers=True)
    different_schema = McpToolInfo.from_wire(
        {**definition, "inputSchema": {"type": "object"}}, allow_http_headers=True
    )
    calls = []
    arguments = {"route": {"region": " 東京 ", "active": False, "partition": -7, "optional": None}}

    async def handler(request):
        message = json.loads(request.content)
        method = message["method"]
        if method == "server/discover":
            result = {"supportedVersions": [MODERN]}
        elif method == "tools/list":
            result = {"tools": [rejected, definition, listed_tool()]}
        else:
            calls.append(message["params"]["arguments"])
            assert message["params"]["arguments"] == arguments
            assert request.headers["mcp-name"] == "=?base64?c3VtIGNhZsOp?="
            assert request.headers["mcp-param-region"] == "=?base64?IOadseS6rCA=?="
            assert request.headers["mcp-param-active"] == "false"
            assert request.headers["mcp-param-partition"] == "-7"
            assert "mcp-param-optional" not in request.headers
            assert request.headers["authorization"] == "Bearer fixture-token"
            result = {"resultType": "complete", "structuredContent": {"sum": 42}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            for installed in (None, different_schema, approved):
                transport = McpHttpTransport(
                    URL,
                    http,
                    network_approval=lambda _: None,
                    bearer_supplier=lambda: "fixture-token",
                    header_tool=installed,
                )
                client = McpClient(
                    transport.exchange,
                    admit=lambda *_: None,
                    http_transport=transport,
                )
                catalog = await client.refresh_tools()
                assert [tool.name for tool in catalog.tools] == ["sum café", "add"]
                if installed != approved:
                    with pytest.raises(ScopeDeniedError):
                        await client.call_tool(
                            approved.name,
                            arguments,
                            pinned_catalog_hash=catalog.fingerprint,
                            pinned_tool_hash=approved.fingerprint,
                        )
                else:
                    result = await client.call_tool(
                        approved.name,
                        arguments,
                        pinned_catalog_hash=catalog.fingerprint,
                        pinned_tool_hash=approved.fingerprint,
                    )
                    assert result.structured_content == {"sum": 42}

    asyncio.run(scenario())
    assert calls == [arguments]


def test_mirrored_header_names_are_unique_case_insensitively():
    definition = listed_tool()
    for prop, header in (("a", "Partition"), ("b", "partition")):
        definition["inputSchema"]["properties"][prop]["x-mcp-header"] = header
    with pytest.raises(InvalidMcpHeaderAnnotation):
        McpToolInfo.from_wire(definition, allow_http_headers=True)


@pytest.mark.parametrize("value", [1 << 53, True])
def test_mirrored_integer_rejects_unsafe_values(value):
    definition = listed_tool()
    definition["inputSchema"]["properties"]["a"]["x-mcp-header"] = "Partition"
    tool = McpToolInfo.from_wire(definition, allow_http_headers=True)
    with pytest.raises(InvalidInputError):
        mirrored_headers(tool.header_parameters, {"a": value})


@pytest.mark.parametrize(
    ("fault", "failure", "rounds", "prompts"),
    [
        (None, None, 2, 1),
        ("disabled", CapabilityUnavailableError, 1, 0),
        ("wrong-tool", ScopeDeniedError, 0, 0),
        ("sampling", ScopeDeniedError, 1, 0),
        ("decline", ScopeDeniedError, 1, 1),
        ("wrong-field", ScopeDeniedError, 1, 0),
        ("url-form", ScopeDeniedError, 1, 0),
        ("state-type", ScopeDeniedError, 1, 0),
        ("admission-revoked", ScopeDeniedError, 1, 1),
        ("another-form", McpProtocolError, 2, 1),
        ("lost-reply", IndeterminateExecutionError, 2, 1),
    ],
)
def test_http_mcp_confirmation_is_explicit_pinned_and_single_round(fault, failure, rounds, prompts):
    definition = listed_tool()
    original = {"a": 19, "b": 23}
    sent = []
    asked = []
    admissions = []
    state = "opaque: 東京 "
    field = "other" if fault == "wrong-field" else "confirmed"
    challenge = {
        "resultType": "input_required",
        "requestState": 3 if fault == "state-type" else state,
        "inputRequests": {
            "verify": {
                "method": "sampling/createMessage" if fault == "sampling" else "elicitation/create",
                "params": {
                    "mode": "url" if fault == "url-form" else "form",
                    "message": "Show the computed sum?",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {field: {"type": "boolean", "default": True}},
                        "required": [field],
                    },
                },
            },
        },
    }

    async def peer(request):
        message = json.loads(request.content)
        method = message["method"]
        if method == "server/discover":
            result = {"supportedVersions": [MODERN]}
        elif method == "tools/list":
            result = {"tools": [definition]}
        else:
            assert method == "tools/call"
            sent.append(message)
            params = message["params"]
            assert params["arguments"] == {"a": 19, "b": 23}
            expected = {} if fault == "disabled" else {"elicitation": {"form": {}}}
            assert params["_meta"]["io.modelcontextprotocol/clientCapabilities"] == expected
            if len(sent) == 1:
                assert "requestState" not in params and "inputResponses" not in params
                result = challenge
            else:
                assert params["requestState"] == state
                assert params["inputResponses"] == {
                    "verify": {"action": "accept", "content": {"confirmed": True}},
                }
                assert sent[0]["id"] != message["id"]
                if fault == "lost-reply":
                    raise httpx.ReadTimeout("fixture lost continuation reply")
                result = (
                    challenge
                    if fault == "another-form"
                    else {"resultType": "complete", "structuredContent": {"sum": 42}}
                )
        return respond(request, result)

    async def approve(prompt):
        asked.append(prompt)
        assert prompt == McpConfirmationPrompt("add", "confirmed", "Show the computed sum?")
        original["a"] = 500
        return fault != "decline"

    def admit(tool, arguments):
        admissions.append((tool.name, dict(arguments)))
        if fault == "admission-revoked" and asked:
            raise ScopeDeniedError("Fixture withdrew tool admission")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(peer)) as http:
            transport = McpHttpTransport(URL, http, network_approval=lambda _: None)
            client = McpClient(transport.exchange, admit=admit, http_transport=transport)
            catalog = await client.refresh_tools()
            tool = catalog.tool("add")
            confirmation = McpConfirmation(
                "0" * 64 if fault == "wrong-tool" else tool.fingerprint,
                tool.to_capability(server_slug="fixture", effect_class="read_only"),
                "confirmed",
                approve,
            )
            call = client.call_tool(
                "add",
                original,
                pinned_catalog_hash=catalog.fingerprint,
                pinned_tool_hash=tool.fingerprint,
                confirmation=None if fault == "disabled" else confirmation,
            )
            if failure is None:
                assert (await call).structured_content == {"sum": 42}
            else:
                with pytest.raises(failure):
                    await call

    asyncio.run(scenario())
    assert len(sent) == rounds
    assert len(asked) == prompts
    count = 2 if rounds == 2 or fault == "admission-revoked" else rounds
    assert admissions == [("add", {"a": 19, "b": 23})] * count


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("changed", True),
        ("graceful", False),
        ("idle", False),
        ("wrong-id", McpProtocolError),
        ("no-ack", McpProtocolError),
        ("overbroad", McpProtocolError),
        ("eof", IndeterminateExecutionError),
        ("redirect", ScopeDeniedError),
        ("secret", ScopeDeniedError),
    ],
)
def test_http_catalog_watch_bounds_and_correlates_the_stream(fault, expected):
    calls = []
    meta = {"io.modelcontextprotocol/subscriptionId": 1}
    ack = {
        "jsonrpc": "2.0",
        "method": "notifications/subscriptions/acknowledged",
        "params": {"_meta": meta, "notifications": {"toolsListChanged": True}},
    }
    event = {"jsonrpc": "2.0", "method": TOOLS_CHANGED, "params": {"_meta": meta}}

    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            first = event if fault == "no-ack" else ack
            if fault == "overbroad":
                ack["params"]["notifications"]["promptsListChanged"] = True
            frames = [first]
            if fault in {"changed", "wrong-id", "secret"}:
                if fault == "wrong-id":
                    event["params"] = {"_meta": {"io.modelcontextprotocol/subscriptionId": 2}}
                if fault == "secret":
                    event["params"]["hidden"] = "fixture-secret"
                frames.append(event)
            if fault == "graceful":
                frames.append({"jsonrpc": "2.0", "id": 1, "result": {"_meta": meta}})
            for frame in frames:
                wire = b"data: " + json.dumps(frame).encode() + b"\r\n\r\n"
                for cut in range(0, len(wire), 11):
                    yield wire[cut : cut + 11]
            if fault == "idle":
                await asyncio.Future()

        async def aclose(self):
            self.closed = True

    stream = Stream()

    def serve(request):
        assert request.headers["Mcp-Method"] == TOOLS_LISTEN
        assert request.headers["Authorization"] == "Bearer fixture-secret"
        assert json.loads(request.content)["params"]["notifications"] == {"toolsListChanged": True}
        calls.append(request.method)
        return httpx.Response(
            307 if fault == "redirect" else 200,
            headers={"Content-Type": "text/event-stream", "Location": "https://other.example/mcp"},
            stream=stream,
        )

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
            transport = McpHttpTransport(
                URL,
                http,
                network_approval=lambda url: None,
                bearer_supplier=lambda: "fixture-secret",
            )
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": TOOLS_LISTEN,
                "params": {
                    "notifications": {"toolsListChanged": True},
                    "_meta": {"io.modelcontextprotocol/protocolVersion": MODERN},
                },
            }
            if isinstance(expected, bool):
                reply = await transport.listen_tools(request, window_ms=250)
                assert reply["result"] == {"changed": expected}
            else:
                with pytest.raises(expected):
                    await transport.listen_tools(request, window_ms=250)
        assert calls == ["POST"] and stream.closed

    run(check())
