"""One explicitly approved boolean elicitation on a modern MCP tool call."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.tools.capability_contracts import ToolCapabilityDescriptor

_FIELD = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class McpConfirmationPrompt:
    """Untrusted plain text from the selected tool, with no opaque server state."""

    tool: str
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class McpConfirmation:
    """Controller-owned approval for one boolean field on an exact tool definition."""

    tool_fingerprint: str
    capability: ToolCapabilityDescriptor
    field: str
    approve: Callable[[McpConfirmationPrompt], Awaitable[bool]]

    def __post_init__(self) -> None:
        if (
            not _DIGEST.fullmatch(self.tool_fingerprint)
            or self.capability.protocol != "mcp"
            or self.capability.effect_class != "read_only"
            or not _FIELD.fullmatch(self.field)
            or not callable(self.approve)
        ):
            raise InvalidInputError("Invalid MCP confirmation policy")

    async def answer(self, tool: str, result: dict[str, Any]) -> dict[str, Any]:
        requests = result.get("inputRequests")
        if not isinstance(requests, dict) or len(requests) != 1:
            raise ScopeDeniedError("MCP confirmation requires one input request")
        key, request = next(iter(requests.items()))
        if (
            not isinstance(key, str)
            or len(key) > 256
            or not isinstance(request, dict)
            or request.get("method") != "elicitation/create"
            or not isinstance(request.get("params"), dict)
        ):
            raise ScopeDeniedError("MCP confirmation does not permit this input request")
        params = request["params"]
        message = params.get("message")
        if (
            params.get("mode", "form") != "form"
            or set(params) - {"mode", "message", "requestedSchema", "_meta"}
            or not isinstance(message, str)
            or not 1 <= len(message) <= 1024
            or any(ord(c) < 32 and c not in "\n\t" for c in message)
        ):
            raise ScopeDeniedError("MCP confirmation requires a bounded form prompt")
        self._check_schema(params.get("requestedSchema"))

        state = result.get("requestState")
        if "requestState" in result and (not isinstance(state, str) or len(state) > 16384):
            raise ScopeDeniedError("MCP confirmation state is outside its bound")
        if await self.approve(McpConfirmationPrompt(tool, self.field, message)) is not True:
            raise ScopeDeniedError("MCP confirmation was not granted")
        continuation: dict[str, Any] = {
            "inputResponses": {key: {"action": "accept", "content": {self.field: True}}}
        }
        if state is not None:
            continuation["requestState"] = state
        return continuation

    def _check_schema(self, schema: object) -> None:
        if (
            not isinstance(schema, dict)
            or schema.get("type") != "object"
            or set(schema) - {"type", "properties", "required", "additionalProperties"}
            or schema.get("required", [self.field]) not in ([], [self.field])
            or type(schema.get("additionalProperties", False)) is not bool
        ):
            raise ScopeDeniedError("MCP confirmation requires a single boolean field")
        properties = schema.get("properties")
        if not isinstance(properties, dict) or set(properties) != {self.field}:
            raise ScopeDeniedError("MCP confirmation field differs from the approval")
        field = properties[self.field]
        if (
            not isinstance(field, dict)
            or field.get("type") != "boolean"
            or set(field) - {"type", "title", "description", "default"}
            or type(field.get("default", False)) is not bool
        ):
            raise ScopeDeniedError("MCP confirmation requires a single boolean field")
