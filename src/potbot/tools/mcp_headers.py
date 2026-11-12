"""Schema-bound header values for modern MCP Streamable HTTP."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass

from potbot.domain.errors import InvalidInputError, ToolFailureError

_ANNOTATION = "x-mcp-header"
_TOKEN = re.compile(r"[!#$%&'*+.^_\x60|~0-9A-Za-z-]{1,128}")
_PRIMITIVES = ("string", "integer", "boolean")
_MAX_SAFE_INTEGER = (1 << 53) - 1
_MAX_PARAMETER_HEADERS = 32
_MAX_PARAMETER_BYTES = 8192


class InvalidMcpHeaderAnnotation(ToolFailureError):
    """A tool with an invalid HTTP annotation must be excluded from discovery."""


@dataclass(frozen=True, slots=True)
class HeaderParameter:
    path: tuple[str, ...]
    name: str
    kind: str

    def extract(self, arguments: dict[str, object]) -> str | None:
        value: object = arguments
        for key in self.path:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        if value is None:
            return None
        if self.kind == "string" and isinstance(value, str):
            return encode_header_value(value)
        if self.kind == "boolean" and type(value) is bool:
            return "true" if value else "false"
        if (
            self.kind == "integer"
            and (type(value) is int or (type(value) is float and value.is_integer()))
            and abs(value) <= _MAX_SAFE_INTEGER
        ):
            return str(int(value))
        raise InvalidInputError("MCP routing parameter cannot be encoded as declared")


def header_parameters(schema: object) -> tuple[HeaderParameter, ...]:
    """Accept only primitives at direct property paths, including nested objects."""
    stack: list[tuple[object, tuple[str, ...] | None]] = [(schema, ())]
    result: list[HeaderParameter] = []
    names: set[str] = set()
    while stack:
        node, path = stack.pop()
        if isinstance(node, dict):
            if _ANNOTATION in node:
                name = node[_ANNOTATION]
                kind = node.get("type")
                if isinstance(kind, list) and len(kind) == 2 and "null" in kind:
                    kind = kind[0] if kind[1] == "null" else kind[1]
                if (
                    not path
                    or not isinstance(name, str)
                    or not _TOKEN.fullmatch(name)
                    or name.lower() in names
                    or kind not in _PRIMITIVES
                    or len(result) >= _MAX_PARAMETER_HEADERS
                ):
                    raise InvalidMcpHeaderAnnotation("Invalid MCP HTTP routing annotation")
                result.append(HeaderParameter(path, "Mcp-Param-" + name, kind))
                names.add(name.lower())
            for key, value in node.items():
                if key == "properties" and path is not None and isinstance(value, dict):
                    stack.extend((child, (*path, name)) for name, child in value.items())
                else:
                    stack.append((value, None))
        elif isinstance(node, list):
            stack.extend((child, None) for child in node)
    return tuple(result)


def encode_header_value(value: str) -> str:
    if (
        value == value.strip(" \t")
        and all(c == "\t" or " " <= c <= "~" for c in value)
        and not (value.startswith("=?base64?") and value.endswith("?="))
    ):
        return value
    try:
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    except UnicodeError:
        raise InvalidInputError("MCP header value is not valid UTF-8") from None
    return f"=?base64?{encoded}?="


def mirrored_headers(
    parameters: tuple[HeaderParameter, ...], arguments: dict[str, object]
) -> dict[str, str]:
    headers = {}
    size = 0
    for parameter in parameters:
        value = parameter.extract(arguments)
        if value is not None:
            size += len(parameter.name) + len(value) + 4
            if size > _MAX_PARAMETER_BYTES:
                raise InvalidInputError("MCP routing headers exceed the transport budget")
            headers[parameter.name] = value
    return headers
