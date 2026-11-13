"""Conservative Streamable HTTP MCP transport, modern and handshake-era.

No arbitrary URL fetching or credential acquisition occurs here. The transport
must be furnished with an HTTP client and a controller-owned, *enforcing*
network approval hook. The controller's MCP HTTP broker can supply a bound
client with DNS/IP pinning, issuer discovery, consent and scoped credentials.
"""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    ScopeDeniedError,
)
from potbot.tools.mcp_headers import encode_header_value, mirrored_headers
from potbot.tools.mcp_protocol import (
    MAX_NOTIFICATION_MESSAGES,
    MAX_WIRE_BYTES,
    MODERN,
    TOOLS_CHANGED,
    TOOLS_LISTEN,
    McpProtocolError,
    McpToolInfo,
    decode_reply,
    encoded_frame,
    parse_wire,
)


def _contains_secret(document: object, secret: str) -> bool:
    pending = [document]
    while pending:
        value = pending.pop()
        if isinstance(value, str) and secret in value:
            return True
        if isinstance(value, dict):
            pending.extend(value)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return False


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
        header_tool: McpToolInfo | None = None,
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
        self._header_tool = header_tool

    def accepts_header_tool(self, tool: McpToolInfo) -> bool:
        return self._header_tool is not None and self._header_tool.fingerprint == tool.fingerprint

    def _authorize(self, headers: dict[str, str]) -> str | None:
        if self.network_approval is None:
            raise ScopeDeniedError("No controller-qualified MCP network transport")
        self.network_approval(self.endpoint)
        token = None
        if self.bearer_supplier is not None:
            token = self.bearer_supplier()
            if (
                not isinstance(token, str)
                or not token
                or any(ord(c) < 33 or ord(c) > 126 for c in token)
            ):
                raise ScopeDeniedError("MCP credential broker returned an invalid secret")
            headers["Authorization"] = "Bearer " + token
        return token

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
        method = message.get("method")
        if not isinstance(method, str) or not method:
            raise InvalidInputError("MCP POST has no valid method")
        if method == TOOLS_LISTEN:
            raise InvalidInputError("MCP catalogue subscriptions require a bounded watch")
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
                params = message["params"]
                name = params.get("name")
                if not isinstance(name, str) or not 1 <= len(name) <= 128:
                    raise InvalidInputError("MCP tool has no valid name for HTTP routing")
                headers["Mcp-Name"] = encode_header_value(name)
                if self._header_tool is not None and self._header_tool.name == name:
                    arguments = params.get("arguments")
                    if not isinstance(arguments, dict):
                        raise InvalidInputError("MCP routing parameters require tool arguments")
                    headers.update(mirrored_headers(self._header_tool.header_parameters, arguments))
        elif self._legacy_version is not None:
            headers["MCP-Protocol-Version"] = self._legacy_version
            if self._session_id is not None:
                headers["Mcp-Session-Id"] = self._session_id
        token = self._authorize(headers)
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
                    if token is not None and token.encode("ascii") in body:
                        raise ScopeDeniedError("MCP response contains its authorization secret")
                    kind = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if kind == "application/json":
                        document = parse_wire(body)
                    elif kind == "text/event-stream":
                        if type(message.get("id")) is not int:
                            raise McpProtocolError("MCP SSE response has no corresponding request")
                        document = self._sse_result(body, message["id"])
                    else:
                        raise McpProtocolError("Unsupported MCP HTTP content type")
                    if token is not None and _contains_secret(document, token):
                        raise ScopeDeniedError("MCP response contains its authorization secret")
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
                                if token is not None and token in session:
                                    raise ScopeDeniedError(
                                        "MCP session contains its authorization secret"
                                    )
                                if not 1 <= len(session) <= 256 or any(
                                    ord(x) < 33 or ord(x) > 126 for x in session
                                ):
                                    raise McpProtocolError("Invalid legacy MCP session identifier")
                                self._session_id = session
                    return document
        except TimeoutError:
            raise IndeterminateExecutionError(
                "MCP HTTP deadline exceeded; write status unknown"
            ) from None
        except (OSError, ConnectionError, httpx.TransportError):
            raise IndeterminateExecutionError(
                "MCP HTTP connection failed; write status unknown"
            ) from None

    async def listen_tools(self, request: dict[str, Any], *, window_ms: int) -> dict[str, Any]:
        """Observe at most one catalogue invalidation within the admitted window."""
        request_id = request.get("id")
        requested = request.get("params")
        version = requested.get("_meta") if isinstance(requested, dict) else None
        if (
            request.get("jsonrpc") != "2.0"
            or not isinstance(requested, dict)
            or set(requested) != {"notifications", "_meta"}
            or not isinstance(requested["notifications"], dict)
            or set(requested["notifications"]) != {"toolsListChanged"}
            or requested["notifications"]["toolsListChanged"] is not True
            or not isinstance(version, dict)
            or version.get("io.modelcontextprotocol/protocolVersion") != MODERN
            or type(request_id) is not int
            or request_id <= 0
            or request.get("method") != TOOLS_LISTEN
            or type(window_ms) is not int
            or not 250 <= window_ms <= 5000
        ):
            raise InvalidInputError("Invalid MCP catalogue watch")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MODERN,
            "Mcp-Method": TOOLS_LISTEN,
        }
        token = self._authorize(headers)
        acknowledged = False
        changed = False
        deadline = asyncio.timeout(min(self.deadline_s, window_ms / 1000))
        try:
            async with deadline:
                async with self.client.stream(
                    "POST",
                    self.endpoint,
                    headers=headers,
                    content=encoded_frame(request),
                    follow_redirects=False,
                ) as response:
                    if response.status_code in (401, 403) or 300 <= response.status_code < 400:
                        raise ScopeDeniedError("MCP subscription authorization was refused")
                    kind = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if response.status_code != 200 or kind != "text/event-stream":
                        raise McpProtocolError("MCP catalogue subscription requires an SSE stream")
                    async for document in _subscription_frames(response):
                        if token is not None and _contains_secret(document, token):
                            raise ScopeDeniedError(
                                "MCP subscription contains its authorization secret"
                            )
                        if "id" in document:
                            result = decode_reply(document, request_id)
                            if (
                                not acknowledged
                                or set(result) - {"_meta"}
                                or not _matches_subscription(result, request_id)
                            ):
                                raise McpProtocolError("Invalid MCP subscription completion")
                            break
                        params = document.get("params")
                        if not isinstance(params, dict) or not _matches_subscription(
                            params, request_id
                        ):
                            raise McpProtocolError(
                                "MCP subscription notification has the wrong identity"
                            )
                        method = document.get("method")
                        if not acknowledged:
                            if (
                                method != "notifications/subscriptions/acknowledged"
                                or params.get("notifications") != {"toolsListChanged": True}
                                or params["notifications"]["toolsListChanged"] is not True
                            ):
                                raise McpProtocolError(
                                    "MCP provider did not acknowledge toolsListChanged"
                                )
                            acknowledged = True
                        elif method == TOOLS_CHANGED:
                            changed = True
                            break
                        else:
                            raise McpProtocolError(
                                "Unexpected MCP catalogue subscription notification"
                            )
                    else:
                        raise IndeterminateExecutionError(
                            "MCP catalogue subscription ended abruptly"
                        )
        except TimeoutError:
            if not acknowledged or not deadline.expired():
                raise IndeterminateExecutionError(
                    "MCP catalogue subscription timed out before a confirmed observation"
                ) from None
        except (OSError, ConnectionError, httpx.TransportError):
            raise IndeterminateExecutionError("MCP catalogue subscription was lost") from None
        return {"jsonrpc": "2.0", "id": request_id, "result": {"changed": changed}}


def _matches_subscription(fields: dict[str, Any], request_id: int) -> bool:
    meta = fields.get("_meta")
    ident = meta.get("io.modelcontextprotocol/subscriptionId") if isinstance(meta, dict) else None
    return type(ident) is int and ident == request_id


async def _subscription_frames(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Parse event boundaries incrementally with the ordinary MCP wire budget."""
    pending = bytearray()
    data: list[bytes] = []
    size = 0
    count = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_WIRE_BYTES:
            raise McpProtocolError("MCP subscription exceeds the wire budget")
        lines = chunk.split(b"\n")
        for piece in lines[:-1]:
            pending.extend(piece)
            line = bytes(pending).removesuffix(b"\r")
            pending.clear()
            if line.startswith(b"data:"):
                data.append(line[5:].removeprefix(b" "))
            elif not line and data:
                count += 1
                if count > MAX_NOTIFICATION_MESSAGES + 1:
                    raise McpProtocolError("MCP subscription exceeds the notification budget")
                yield parse_wire(b"\n".join(data))
                data.clear()
        pending.extend(lines[-1])
