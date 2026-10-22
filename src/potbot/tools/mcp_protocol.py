"""Bounded MCP tool discovery and controller-admitted calls (protocol eras 2025/2026).

This module deliberately DOES NOT grant network, process or tool permissions.
A transport is supplied by a controller-owned gateway, and every tools/call
requires a trusted admission callback. Server-advertised annotations (including
readOnlyHint) are never an authorization or effect classification.

The 2026-07-28 era is stateless; older 2025-era peers use initialize/initialized.
Sampling, elicitation, roots, MRTR and tasks are not implemented in this slice.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
    ToolFailureError,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools.capability_contracts import (
    MCP_TOOL_DEFINITION_TYPE,
    ToolCapabilityDescriptor,
    ToolInvocationIntent,
    ToolObservation,
)

MODERN = "2026-07-28"
LEGACY = "2025-11-25"
SUPPORTED_LEGACY = frozenset({LEGACY, "2025-06-18", "2025-03-26"})
MAX_WIRE_BYTES = 2 * 1024 * 1024
MAX_TOOLS = 256
MAX_PAGES = 32
MAX_NOTIFICATION_MESSAGES = 32
_MAX_SCHEMA_BYTES = 128 * 1024
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_ALLOWED_EFFECTS = frozenset(
    {"read_only", "idempotent_write", "non_idempotent_write", "destructive"}
)


class McpProtocolError(ToolFailureError):
    """Invalid peer data or protocol negotiation; never auto-retry a write."""


class McpRpcError(ToolFailureError):
    """A valid JSON-RPC error returned by a peer."""

    def __init__(self, code: int, message: str) -> None:
        self.rpc_code = code
        # Never expose attacker-controlled text or credentials to logs.
        super().__init__(f"MCP peer returned JSON-RPC error {code}")
        self.remote_message = message[:1024]


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise McpProtocolError("Duplicate key in MCP message")
        output[key] = value
    return output


def _bad_constant(value: str) -> Any:
    raise McpProtocolError("Nonfinite number in MCP message")


def parse_wire(raw: bytes) -> dict[str, Any]:
    """Reject malformed or unbounded JSON before inspecting a peer message."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_WIRE_BYTES:
        raise McpProtocolError("Invalid or oversized MCP frame")
    try:
        document = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_unique_pairs,
            parse_constant=_bad_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        raise McpProtocolError("Malformed MCP JSON frame") from error
    if not isinstance(document, dict) or document.get("jsonrpc") != "2.0":
        raise McpProtocolError("MCP JSON-RPC object is invalid")
    return document


def encoded_frame(document: Mapping[str, Any]) -> bytes:
    try:
        data = canonical_json(dict(document)).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as error:
        raise InvalidInputError("MCP request is not finite JSON") from error
    if len(data) > MAX_WIRE_BYTES:
        raise InvalidInputError("MCP request exceeds the maximum frame size")
    return data


def decode_reply(document: Mapping[str, Any], request_id: int) -> dict[str, Any]:
    if document.get("jsonrpc") != "2.0" or type(document.get("id")) is not int:
        raise McpProtocolError("Malformed MCP JSON-RPC reply")
    if document["id"] != request_id:
        raise McpProtocolError("MCP request/reply identity mismatch")
    if ("error" in document) == ("result" in document):
        raise McpProtocolError("MCP response must contain exactly one result or error")
    if "error" in document:
        error = document["error"]
        if (
            not isinstance(error, dict)
            or type(error.get("code")) is not int
            or not isinstance(error.get("message"), str)
        ):
            raise McpProtocolError("Malformed JSON-RPC error")
        raise McpRpcError(error["code"], error["message"])
    result = document["result"]
    if not isinstance(result, dict):
        raise McpProtocolError("Unexpected non-object MCP result")
    return result


def _local_references_only(document: Any) -> None:
    """Refuse remote schema loads or unbounded schema graphs in controller space."""
    stack: list[tuple[Any, int]] = [(document, 0)]
    inspected = 0
    while stack:
        node, depth = stack.pop()
        inspected += 1
        if depth > 32 or inspected > 4096:
            raise McpProtocolError("MCP JSON Schema nesting or complexity exceeds policy")
        if isinstance(node, dict):
            for key, child in node.items():
                if key in ("$ref", "$dynamicRef", "$recursiveRef"):
                    if not isinstance(child, str) or not child.startswith("#/") or len(child) > 512:
                        raise McpProtocolError(
                            "Remote or unbounded MCP schema references are forbidden"
                        )
                if key == "x-mcp-header":
                    # Header-mirrored schemas require v2's protocol-level verified
                    # encoding and annotation location checks; fail closed here.
                    raise CapabilityUnavailableError(
                        "MCP header-mirrored tool inputs are not enabled"
                    )
                stack.append((child, depth + 1))
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in node)


def _schema(record: object, label: str) -> str:
    if not isinstance(record, dict) or record.get("type") != "object":
        raise McpProtocolError(f"{label} must be an object JSON Schema")
    try:
        output = canonical_json(record)
    except (InvalidInputError, ValueError, RecursionError) as error:
        raise McpProtocolError(f"{label} is not finite JSON") from error
    if len(output.encode("utf-8")) > _MAX_SCHEMA_BYTES:
        raise McpProtocolError("MCP tool schema too large")
    _local_references_only(record)
    try:
        from jsonschema.validators import validator_for
    except ImportError as error:
        raise CapabilityUnavailableError(
            "MCP schema checking needs the Potbot semantics extra"
        ) from error
    try:
        validator = validator_for(record)
        validator.check_schema(record)
    except Exception as error:
        raise McpProtocolError(f"{label} is not a valid JSON Schema") from error
    return output


def validate_arguments(tool: McpToolInfo, arguments: object) -> None:
    if not isinstance(arguments, dict):
        raise InvalidInputError("MCP tool arguments must be a JSON object")
    if len(encoded_frame(arguments)) > MAX_WIRE_BYTES // 2:
        raise InvalidInputError("MCP tool arguments too large")
    try:
        from jsonschema.validators import validator_for
    except ImportError as error:
        raise CapabilityUnavailableError(
            "MCP schema checking needs the Potbot semantics extra"
        ) from error
    schema = json.loads(tool.input_schema_json)
    _local_references_only(schema)
    try:
        validator_for(schema)(schema).validate(arguments)
    except Exception as error:
        raise InvalidInputError("Arguments do not satisfy the MCP tool schema") from error


@dataclass(frozen=True, slots=True)
class McpToolInfo:
    name: str
    description: str
    input_schema_json: str
    output_schema_json: str | None = None

    @classmethod
    def from_wire(cls, document: object) -> McpToolInfo:
        if not isinstance(document, dict):
            raise McpProtocolError("Malformed MCP tool definition")
        name = document.get("name")
        description = document.get("description", "No description supplied")
        if not isinstance(name, str) or not 1 <= len(name) <= 128 or any(ord(x) < 32 for x in name):
            raise McpProtocolError("MCP tool has no valid name")
        if (
            not isinstance(description, str)
            or len(description) > 4096
            or any(ord(x) < 32 and x not in "\n\t" for x in description)
        ):
            raise McpProtocolError("MCP tool has an invalid description")
        input_schema = _schema(document.get("inputSchema"), "MCP input")
        output_schema = document.get("outputSchema")
        if output_schema is not None:
            output_schema = _schema(output_schema, "MCP output")
        return cls(name, description, input_schema, output_schema)

    @property
    def fingerprint(self) -> str:
        return content_hash(
            {
                "name": self.name,
                "description": self.description,
                "input_schema_json": self.input_schema_json,
                "output_schema_json": self.output_schema_json,
            }
        )

    def to_capability(
        self,
        *,
        server_slug: str,
        effect_class: str,
        input_type_ref: str = "synny://type/potbot-data-record",
        output_type_ref: str = "synny://type/potbot-data-record",
    ) -> ToolCapabilityDescriptor:
        """Create public metadata only; effects MUST come from operator policy."""
        if not _IDENTIFIER.fullmatch(server_slug) or effect_class not in _ALLOWED_EFFECTS:
            raise InvalidInputError("MCP server key/effect must be controller-curated")
        name_hash = hashlib.sha256(self.name.encode("utf-8")).hexdigest()[:16]
        return ToolCapabilityDescriptor(
            name=f"mcp.{server_slug}.{name_hash}",
            definition_type_ref=MCP_TOOL_DEFINITION_TYPE,
            input_type_ref=input_type_ref,
            output_type_ref=output_type_ref,
            protocol="mcp",
            effect_class=effect_class,
            input_schema_json=self.input_schema_json,
            output_schema_json=self.output_schema_json or canonical_json({"type": "object"}),
            description=(" ".join(self.description.split())[:3900] or "MCP tool definition"),
        )


@dataclass(frozen=True, slots=True)
class McpCatalog:
    tools: tuple[McpToolInfo, ...]
    fingerprint: str

    def tool(self, name: str) -> McpToolInfo:
        for tool in self.tools:
            if tool.name == name:
                return tool
        raise ScopeDeniedError("Tool was not listed in the pinned MCP catalog")


@dataclass(frozen=True, slots=True)
class McpCallResult:
    tool_name: str
    content: tuple[dict[str, Any], ...]
    structured_content: dict[str, Any] | None
    is_error: bool
    result_hash: str

    @classmethod
    def from_wire(cls, name: str, result: dict[str, Any]) -> McpCallResult:
        # 2026-era MRTR/Task extensions are intentionally not silently followed.
        if result.get("resultType") == "input_required" or "inputRequests" in result:
            raise CapabilityUnavailableError(
                "Interactive MCP tool results require a separate approved flow"
            )
        raw_content = result.get("content", [])
        if (
            not isinstance(raw_content, list)
            or len(raw_content) > 128
            or any(not isinstance(x, dict) for x in raw_content)
        ):
            raise McpProtocolError("MCP tool returned malformed content blocks")
        structured = result.get("structuredContent")
        if structured is not None and not isinstance(structured, dict):
            raise McpProtocolError("MCP structured tool output must be an object")
        is_error = result.get("isError", False)
        if type(is_error) is not bool:
            raise McpProtocolError("MCP isError must be boolean")
        payload = {"content": raw_content, "structuredContent": structured, "isError": is_error}
        if len(encoded_frame(payload)) > MAX_WIRE_BYTES:
            raise McpProtocolError("MCP result exceeds the output limit")
        return cls(name, tuple(raw_content), structured, is_error, content_hash(payload))

    def to_observation(
        self, *, intent: ToolInvocationIntent, capability: ToolCapabilityDescriptor
    ) -> ToolObservation:
        """Unverified result observation; persistence and approval are separate."""
        intent.assert_capability(capability)
        if capability.protocol != "mcp":
            raise ScopeDeniedError("MCP observation must refer to an MCP capability")
        return ToolObservation(
            invocation_hash=intent.fingerprint,
            output_type_ref=capability.output_type_ref,
            status="failed" if self.is_error else "succeeded",
            output_hash=None if self.is_error else self.result_hash,
            artifact_sha256=None,
            error_code="mcp_tool_reported_error" if self.is_error else None,
        )


# Transports are given only a method, params, an ID and expected-reply mode;
# no authorization is embedded in, or inferred from, the JSON-RPC fields.
Exchange = Callable[[dict[str, Any], bool], Awaitable[dict[str, Any] | None]]
Admission = Callable[[McpToolInfo, Mapping[str, Any]], None]


class McpClient:
    """Protocol state and bounded tool listing on a trusted transport.

    The transport is NOT an executor. An executing controller must pre-authorize
    both its transport and each side-effecting tool call via ``admit``.
    """

    def __init__(self, exchange: Exchange, *, admit: Admission | None = None) -> None:
        self._exchange = exchange
        self._admit = admit
        self._next_id = 0
        self._version: str | None = None
        self._catalog: McpCatalog | None = None

    @property
    def protocol_version(self) -> str | None:
        return self._version

    @property
    def catalog(self) -> McpCatalog | None:
        return self._catalog

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        fields: dict[str, Any] = {} if params is None else dict(params)
        if self._version == MODERN or (self._version is None and method == "server/discover"):
            meta = {
                "io.modelcontextprotocol/protocolVersion": MODERN,
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {"name": "potbot", "version": "0.5.0"},
            }
            fields["_meta"] = meta
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": fields}
        response = await self._exchange(request, True)
        if response is None:
            raise McpProtocolError("Missing MCP response")
        return decode_reply(response, request_id)

    async def _notify(self, method: str) -> None:
        await self._exchange({"jsonrpc": "2.0", "method": method}, False)

    async def connect(self) -> str:
        if self._version is not None:
            return self._version
        try:
            discovered = await self._rpc("server/discover")
        except McpRpcError as error:
            if error.rpc_code != -32601:
                raise
        else:
            versions = discovered.get("supportedVersions")
            if not isinstance(versions, list) or MODERN not in versions:
                raise McpProtocolError("Unsupported MCP modern protocol advertisement")
            self._version = MODERN
            return MODERN
        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": LEGACY,
                "capabilities": {},
                "clientInfo": {"name": "potbot", "version": "0.5.0"},
            },
        )
        version = result.get("protocolVersion")
        if version not in SUPPORTED_LEGACY:
            raise McpProtocolError("Server negotiated an unsupported legacy MCP revision")
        await self._notify("notifications/initialized")
        self._version = version
        return version

    async def refresh_tools(self) -> McpCatalog:
        if self._version is None:
            await self.connect()
        cursor: str | None = None
        seen: set[str] = set()
        names: set[str] = set()
        tools: list[McpToolInfo] = []
        for _ in range(MAX_PAGES):
            params = {} if cursor is None else {"cursor": cursor}
            page = await self._rpc("tools/list", params)
            listed = page.get("tools")
            if not isinstance(listed, list):
                raise McpProtocolError("MCP tools/list returned no valid tools array")
            for item in listed:
                tool = McpToolInfo.from_wire(item)
                if tool.name in names:
                    raise McpProtocolError("Duplicate MCP tool name across list pages")
                names.add(tool.name)
                tools.append(tool)
                if len(tools) > MAX_TOOLS:
                    raise McpProtocolError("MCP tool count exceeds the discovery budget")
            next_cursor = page.get("nextCursor")
            if next_cursor is None:
                catalog = McpCatalog(
                    tuple(tools),
                    content_hash(
                        {"protocol_version": self._version, "tools": [x.fingerprint for x in tools]}
                    ),
                )
                self._catalog = catalog
                return catalog
            if (
                not isinstance(next_cursor, str)
                or not 1 <= len(next_cursor) <= 2048
                or next_cursor in seen
            ):
                raise McpProtocolError("MCP listing cursor is invalid or cycling")
            seen.add(next_cursor)
            cursor = next_cursor
        raise McpProtocolError("MCP listing exceeds its pagination budget")

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        pinned_catalog_hash: str,
        pinned_tool_hash: str,
    ) -> McpCallResult:
        if self._catalog is None or self._catalog.fingerprint != pinned_catalog_hash:
            raise StateMismatchError("MCP catalog is missing or has changed since admission")
        tool = self._catalog.tool(name)
        if tool.fingerprint != pinned_tool_hash:
            raise StateMismatchError("MCP tool signature changed since admission")
        validate_arguments(tool, arguments)
        if self._admit is None:
            raise ScopeDeniedError("No controller-owned MCP dispatch authorization is installed")
        # Must raise on denial. Never accept an affirmative claim in model data.
        self._admit(tool, arguments)
        outcome = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        result = McpCallResult.from_wire(name, outcome)
        if tool.output_schema_json is not None and not result.is_error:
            if result.structured_content is None:
                raise McpProtocolError(
                    "Tool declared an output schema but omitted structuredContent"
                )
            schema = json.loads(tool.output_schema_json)
            try:
                from jsonschema.validators import validator_for

                validator_for(schema)(schema).validate(result.structured_content)
            except ImportError as error:
                raise CapabilityUnavailableError(
                    "MCP output validation requires semantics"
                ) from error
            except Exception as error:
                raise McpProtocolError(
                    "MCP tool result violates its declared output schema"
                ) from error
        return result
