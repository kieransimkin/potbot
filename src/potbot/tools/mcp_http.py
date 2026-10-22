"""Conservative Streamable HTTP MCP transport, modern and handshake-era.

No arbitrary URL fetching or credential acquisition occurs here. The transport
must be furnished with an HTTP client and a controller-owned, *enforcing*
network approval hook. Production enforcement requires a qualified egress proxy
with pinned DNS/IP filtering; syntactic hostname validation is NOT sufficient.
OAuth discovery/consent is not implemented in T02.2's initial slice: the broker
must supply a pre-authorized audience-scoped bearer via an ephemeral callback.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    ScopeDeniedError,
)
from potbot.tools.mcp_protocol import (
    MAX_NOTIFICATION_MESSAGES,
    MAX_WIRE_BYTES,
    MODERN,
    McpProtocolError,
    encoded_frame,
    parse_wire,
)


class McpHttpTransport:
    """Single-request POST/JSON-or-SSE transport with explicit egress admission.

    ``network_approval`` is called before each request. Its implementation MUST
    enforce endpoint, resolved IP, DNS rebinding, network route and workspace
    grants at the OS/network boundary. A callback that merely returns without
    enforcement is NOT a qualified production transport.
    """

    def __init__(
        self,
        endpoint: str,
        http_client: Any,
        *,
        network_approval: Callable[[str], None] | None = None,
        bearer_supplier: Callable[[], str] | None = None,
        deadline_s: float = 30.0,
        allow_test_loopback: bool = False,
    ) -> None:
        try:
            parts = urlsplit(endpoint)
            port = parts.port
        except ValueError as error:
            raise InvalidInputError("Invalid MCP HTTP endpoint") from error
        if (
            not isinstance(endpoint, str)
            or len(endpoint) > 2048
            or parts.scheme not in ("https", "http")
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
            or parts.query
            or any(ord(c) < 33 or ord(c) > 126 for c in endpoint)
        ):
            raise InvalidInputError("MCP endpoint must be a bounded uncredentialed URL")
        if not 0.01 <= deadline_s <= 3600:
            raise InvalidInputError("Invalid MCP HTTP deadline")
        host = parts.hostname
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if parts.scheme != "https" or ip is not None:
            if not (
                allow_test_loopback and parts.scheme == "http" and ip is not None and ip.is_loopback
            ):
                raise ScopeDeniedError("MCP HTTP requires HTTPS and a nonliteral public hostname")
        if port is not None and not 1 <= port <= 65535:
            raise InvalidInputError("Invalid MCP HTTP port")
        if not parts.path or parts.path == "/":
            raise InvalidInputError("MCP endpoint needs an explicit path")
        self.endpoint = endpoint
        self.client = http_client
        self.network_approval = network_approval
        self.bearer_supplier = bearer_supplier
        self.deadline_s = deadline_s
        self._session_id: str | None = None
        self._legacy_version: str | None = None

    @staticmethod
    def _sse_result(body: bytes, request_id: int) -> dict[str, Any]:
        try:
            text = body.decode("utf-8", "strict").replace("\r\n", "\n")
        except UnicodeError as error:
            raise McpProtocolError("Invalid MCP SSE UTF-8") from error
        count = 0
        for event in text.split("\n\n"):
            if not event.strip():
                continue
            data = "\n".join(
                line[5:].lstrip(" ") for line in event.split("\n") if line.startswith("data:")
            )
            if not data:
                continue
            count += 1
            if count > MAX_NOTIFICATION_MESSAGES + 1:
                raise McpProtocolError("MCP SSE notification budget exceeded")
            message = parse_wire(data.encode("utf-8"))
            if "id" not in message and isinstance(message.get("method"), str):
                if not message["method"].startswith("notifications/"):
                    raise McpProtocolError("Unsupported MCP SSE server request")
                continue
            if message.get("id") != request_id:
                raise McpProtocolError("MCP SSE returned the wrong request identity")
            return message
        raise IndeterminateExecutionError("MCP SSE closed without a final response")

    async def exchange(
        self,
        message: dict[str, Any],
        expect_reply: bool,
    ) -> dict[str, Any] | None:
        if self.network_approval is None:
            raise ScopeDeniedError("No controller-qualified MCP network transport")
        # The callback must fail closed for unqualified hosts, routes and scopes.
        self.network_approval(self.endpoint)
        method = message.get("method")
        if not isinstance(method, str) or not method:
            raise InvalidInputError("MCP POST has no valid method")
        modern = method == "server/discover" or (
            isinstance(message.get("params"), dict)
            and isinstance(message["params"].get("_meta"), dict)
            and message["params"]["_meta"].get("io.modelcontextprotocol/protocolVersion") == MODERN
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if modern:
            headers["MCP-Protocol-Version"] = MODERN
            headers["Mcp-Method"] = method
            if method == "tools/call":
                name = message.get("params", {}).get("name")
                if (
                    not isinstance(name, str)
                    or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name) is None
                ):
                    raise ScopeDeniedError(
                        "MCP tool header name needs verified header encoding support"
                    )
                headers["Mcp-Name"] = name
        elif self._legacy_version is not None:
            headers["MCP-Protocol-Version"] = self._legacy_version
            if self._session_id is not None:
                headers["Mcp-Session-Id"] = self._session_id
        if self.bearer_supplier is not None:
            token = self.bearer_supplier()
            if (
                not isinstance(token, str)
                or not token
                or any(ord(c) < 33 or ord(c) > 126 for c in token)
            ):
                raise ScopeDeniedError("MCP credential broker returned an invalid secret")
            headers["Authorization"] = "Bearer " + token
        try:
            # Explicitly disable redirects even if the injected client defaults
            # to follow_redirects=True; never send a bearer to a new host.
            async with asyncio.timeout(self.deadline_s):
                async with self.client.stream(
                    "POST",
                    self.endpoint,
                    headers=headers,
                    content=encoded_frame(message),
                    follow_redirects=False,
                ) as response:
                    status = response.status_code
                    if status in (401, 403):
                        raise ScopeDeniedError("MCP endpoint rejected the scoped authorization")
                    if 300 <= status < 400:
                        raise ScopeDeniedError("MCP redirects are forbidden")
                    if status == 202 and not expect_reply:
                        return None
                    if status >= 500 or status == 429:
                        raise IndeterminateExecutionError(
                            "MCP remote service unavailable; no implicit retry"
                        )
                    if status not in (200, 201, 400, 404):
                        raise McpProtocolError("Unexpected MCP HTTP status")
                    chunks: list[bytes] = []
                    total = 0
                    async for piece in response.aiter_bytes():
                        total += len(piece)
                        if total > MAX_WIRE_BYTES:
                            raise McpProtocolError("MCP HTTP body exceeds the output budget")
                        chunks.append(piece)
                    body = b"".join(chunks)
                    kind = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if kind == "application/json":
                        document = parse_wire(body)
                    elif kind == "text/event-stream":
                        if type(message.get("id")) is not int:
                            raise McpProtocolError("MCP SSE response has no corresponding request")
                        document = self._sse_result(body, message["id"])
                    else:
                        raise McpProtocolError("Unsupported MCP HTTP content type")
                    if not expect_reply:
                        raise McpProtocolError("MCP notification should return 202 with no body")
                    if method == "initialize" and status in (200, 201):
                        # Strictly handshake-era only. No protocol sessions in 2026.
                        result = document.get("result")
                        if isinstance(result, dict) and isinstance(
                            result.get("protocolVersion"), str
                        ):
                            self._legacy_version = result["protocolVersion"]
                            session = response.headers.get("mcp-session-id")
                            if session is not None:
                                if not 1 <= len(session) <= 256 or any(
                                    ord(x) < 33 or ord(x) > 126 for x in session
                                ):
                                    raise McpProtocolError("Invalid legacy MCP session identifier")
                                self._session_id = session
                    return document
        except TimeoutError as error:
            raise IndeterminateExecutionError(
                "MCP HTTP deadline exceeded; write status unknown"
            ) from error
        except (OSError, ConnectionError, httpx.TransportError) as error:
            raise IndeterminateExecutionError(
                "MCP HTTP connection failed; write status unknown"
            ) from error
