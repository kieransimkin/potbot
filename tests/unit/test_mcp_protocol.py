"""T02.1/T02.2 protocol contract tests, independent of network/process launch."""
from __future__ import annotations

import asyncio

import pytest

from potbot.domain.errors import (
    CapabilityUnavailableError, InvalidInputError, ScopeDeniedError, StateMismatchError,
)
from potbot.tools.mcp_protocol import (
    LEGACY, MODERN, McpCallResult, McpClient, McpProtocolError,
    McpRpcError, McpToolInfo, decode_reply, parse_wire, validate_arguments,
)

SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}


def fixture_tool(name="search", *, schema=SCHEMA):
    return {"name": name, "description": "Query documents", "inputSchema": schema,
            "annotations": {"readOnlyHint": True}}


def run(aw):
    return asyncio.run(aw)


def test_mcp_message_robustness():
    assert parse_wire(b'{"jsonrpc":"2.0","id":1,"result":{}}')["id"] == 1
    for text in [b'{"jsonrpc":"2.0","jsonrpc":"2.0"}', b'{"jsonrpc":"2.0","x":NaN}',
                 b'\xff', b'[]', b'{"jsonrpc":"1.0"}', b'']:
        with pytest.raises(McpProtocolError):
            parse_wire(text)
    with pytest.raises(McpProtocolError):
        parse_wire(b'{' + b'"x":' + b'"a"'*2_000_000 + b'}')


def test_reply_id_and_error_qualification():
    with pytest.raises(McpProtocolError):
        decode_reply({"jsonrpc": "2.0", "id": 9, "result": {}}, 1)
    with pytest.raises(McpProtocolError):
        decode_reply({"jsonrpc": "2.0", "id": True, "result": {}}, 1)
    with pytest.raises(McpRpcError) as exc:
        decode_reply({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "x"}}, 1)
    assert exc.value.rpc_code == -32601


def test_schema_validation_and_unsupported_schema():
    tool = McpToolInfo.from_wire(fixture_tool())
    assert tool.name == "search"
    validate_arguments(tool, {"query": "hello"})
    with pytest.raises(InvalidInputError):
        validate_arguments(tool, {"query": 52})
    with pytest.raises(McpProtocolError):
        McpToolInfo.from_wire(fixture_tool(schema={"type": "object", "properties": {"x": {"type": "nonsense"}}}))
    with pytest.raises(McpProtocolError):
        McpToolInfo.from_wire(fixture_tool(schema={"type": "array"}))


def test_untrusted_mcp_effect_annotations_do_not_grant_permissions():
    tool = McpToolInfo.from_wire(fixture_tool())
    with pytest.raises(TypeError):
        tool.to_capability(server_slug="fixture")
    with pytest.raises(InvalidInputError):
        tool.to_capability(server_slug="BAD", effect_class="read_only")
    with pytest.raises(InvalidInputError):
        tool.to_capability(server_slug="fixture", effect_class="inferred")
    descriptor = tool.to_capability(server_slug="fixture", effect_class="non_idempotent_write")
    assert descriptor.effect_class == "non_idempotent_write"
    assert descriptor.name.startswith("mcp.fixture.")
    assert descriptor.protocol == "mcp"


def test_tool_result_and_interactive_request_fail_closed():
    result = McpCallResult.from_wire("add", {"content": [{"type": "text", "text": "7"}]})
    assert result.is_error is False
    assert len(result.result_hash) == 64
    with pytest.raises(CapabilityUnavailableError):
        McpCallResult.from_wire("add", {"resultType": "input_required", "inputRequests": []})
    with pytest.raises(McpProtocolError):
        McpCallResult.from_wire("add", {"content": [123]})


def test_modern_discovery_pagination_and_pinned_call():
    requests = []
    def authorize(tool, arguments):
        assert tool.name == "search"
        assert arguments["query"] == "test"
    async def exchange(row, expect_reply):
        requests.append(row)
        assert expect_reply
        method = row["method"]
        if method == "server/discover":
            result = {"supportedVersions": [MODERN], "capabilities": {"tools": {}}}
        elif method == "tools/list":
            result = {"tools": [fixture_tool()]} if "cursor" in row["params"] else {"tools": [], "nextCursor": "a"}
        else:
            result = {"content": [{"type": "text", "text": "ok"}]}
        return {"jsonrpc": "2.0", "id": row["id"], "result": result}
    async def scenario():
        client = McpClient(exchange, admit=authorize)
        assert await client.connect() == MODERN
        cat = await client.refresh_tools()
        assert len(cat.tools) == 1
        tool = cat.tool("search")
        res = await client.call_tool("search", {"query": "test"},
            pinned_catalog_hash=cat.fingerprint, pinned_tool_hash=tool.fingerprint)
        assert res.content[0]["text"] == "ok"
    run(scenario())
    assert requests[0]["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == MODERN
    assert requests[-1]["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"] == {}


def test_call_refused_without_admission_or_valid_snapshot():
    calls = []
    async def exchange(row, reply):
        calls.append(row["method"])
        result = ({"supportedVersions": [MODERN]} if row["method"] == "server/discover" else
                  {"tools": [fixture_tool()]} if row["method"] == "tools/list" else {"content": []})
        return {"jsonrpc": "2.0", "id": row["id"], "result": result}
    async def scenario():
        client = McpClient(exchange)
        cat = await client.refresh_tools()
        tool = cat.tool("search")
        with pytest.raises(ScopeDeniedError):
            await client.call_tool("search", {"query": "test"},
              pinned_catalog_hash=cat.fingerprint, pinned_tool_hash=tool.fingerprint)
        with pytest.raises(StateMismatchError):
            await client.call_tool("search", {"query": "test"},
              pinned_catalog_hash="a"*64, pinned_tool_hash=tool.fingerprint)
        with pytest.raises(StateMismatchError):
            await client.call_tool("search", {"query": "test"},
              pinned_catalog_hash=cat.fingerprint, pinned_tool_hash="a"*64)
    run(scenario())
    assert "tools/call" not in calls


def test_no_implicit_fallback_for_non_unknown_method():
    async def exchange(row, reply):
        return {"jsonrpc": "2.0", "id": row["id"], "error": {"code": -32022, "message": "unsupported"}}
    with pytest.raises(McpRpcError):
        run(McpClient(exchange).connect())


def test_catalog_duplicate_cycle_limit_and_stale_rejection():
    async def exercise(mode):
        calls = 0
        async def exchange(row, reply):
            nonlocal calls
            calls += 1
            method = row["method"]
            if method == "server/discover":
                result = {"supportedVersions": [MODERN]}
            elif mode == "duplicate":
                result = {"tools": [fixture_tool(), fixture_tool()]}
            else:
                result = {"tools": [], "nextCursor": "same"}
            return {"jsonrpc": "2.0", "id": row["id"], "result": result}
        with pytest.raises(McpProtocolError):
            await McpClient(exchange).refresh_tools()
        assert calls <= 4
    run(exercise("duplicate"))
    run(exercise("cursor"))


def test_mcp_legacy_handshake():
    methods = []
    async def exchange(row, reply):
        methods.append(row["method"])
        if not reply:
            return None
        if row["method"] == "server/discover":
            return {"jsonrpc": "2.0", "id": row["id"], "error": {"code": -32601, "message": "not found"}}
        if row["method"] == "initialize":
            result = {"protocolVersion": LEGACY, "capabilities": {}}
        else:
            result = {"tools": []}
        return {"jsonrpc": "2.0", "id": row["id"], "result": result}
    async def task():
        client = McpClient(exchange)
        assert await client.connect() == LEGACY
        await client.refresh_tools()
    run(task())
    assert methods == ["server/discover", "initialize", "notifications/initialized", "tools/list"]


def test_external_schema_reference_and_header_injection_rejected():
    with pytest.raises(McpProtocolError):
        McpToolInfo.from_wire(fixture_tool(schema={"type": "object", "properties": {
            "x": {"$ref": "https://attacker.example/schema.json"}}}))
    with pytest.raises(CapabilityUnavailableError):
        McpToolInfo.from_wire(fixture_tool(schema={"type": "object", "properties": {
            "x": {"type": "string", "x-mcp-header": "Authorization"}}}))


def test_schema_with_excessive_depth_fails_closed():
    node = {"type": "object"}
    current = node
    for _ in range(40):
        current["properties"] = {"nested": {"type": "object"}}
        current = current["properties"]["nested"]
    with pytest.raises(McpProtocolError):
        McpToolInfo.from_wire(fixture_tool(schema=node))


def test_structured_output_checked_when_server_declares_schema():
    definition = fixture_tool()
    definition["outputSchema"] = {"type": "object", "properties": {"total": {"type": "integer"}}, "required": ["total"]}
    async def exercise(outcome):
        async def exchange(row, reply):
            method = row["method"]
            result = {"supportedVersions": [MODERN]} if method == "server/discover" else (
                {"tools": [definition]} if method == "tools/list" else outcome
            )
            return {"jsonrpc": "2.0", "id": row["id"], "result": result}
        client = McpClient(exchange, admit=lambda tool, args: None)
        cat = await client.refresh_tools()
        return await client.call_tool("search", {"query": "x"},
            pinned_catalog_hash=cat.fingerprint, pinned_tool_hash=cat.tool("search").fingerprint)
    assert run(exercise({"content": [], "structuredContent": {"total": 17}})).structured_content == {"total": 17}
    with pytest.raises(McpProtocolError):
        run(exercise({"content": [], "structuredContent": {"total": "bad"}}))
    with pytest.raises(McpProtocolError):
        run(exercise({"content": []}))


def test_mcp_result_links_to_phase1_unverified_observation():
    from uuid import uuid4
    from potbot.tools.capability_contracts import ToolInvocationIntent
    tool = McpToolInfo.from_wire(fixture_tool())
    capability = tool.to_capability(server_slug="fixture", effect_class="read_only")
    intent = ToolInvocationIntent(
        invocation_id=uuid4(), workspace_id=uuid4(), task_revision_id=uuid4(),
        installation_hash="a" * 64, input_type_ref=capability.input_type_ref,
        input_hash="b" * 64, effect_class=capability.effect_class,
        policy_revision_hash="c" * 64, max_wall_time_ms=3000, max_output_bytes=1024,
        idempotency_key="fixture-call-1",
    )
    observation = McpCallResult.from_wire("search", {"content": [{"type": "text", "text": "ok"}]}).to_observation(
        intent=intent, capability=capability)
    assert observation.invocation_hash == intent.fingerprint
    assert observation.status == "succeeded"
    assert observation.artifact_sha256 is None  # no verified artifact persistence
    failed = McpCallResult.from_wire("search", {"content": [], "isError": True}).to_observation(
        intent=intent, capability=capability)
    assert failed.status == "failed"
    assert failed.output_hash is None
    assert failed.error_code == "mcp_tool_reported_error"
