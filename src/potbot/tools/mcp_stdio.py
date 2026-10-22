"""MCP stdio framing over ALREADY-ADMITTED process pipes.

Does not spawn an executable: the existing Potbot ToolGateway/ProcessSupervisor
must establish an isolated, workspace-bound child and pass its pipes here.
Direct subprocess launching in application code would bypass Potbot's
sandbox authorization and is intentionally absent.
"""

from __future__ import annotations

import asyncio
from typing import Any

from potbot.domain.errors import IndeterminateExecutionError, InvalidInputError
from potbot.tools.mcp_protocol import (
    MAX_NOTIFICATION_MESSAGES,
    MAX_WIRE_BYTES,
    McpProtocolError,
    encoded_frame,
    parse_wire,
)


class McpStdioTransport:
    """Single-flight bounded JSON-RPC transport; no silent replay after EOF."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        request_timeout_s: float = 30.0,
    ) -> None:
        if not isinstance(reader, asyncio.StreamReader) or not hasattr(writer, "drain"):
            raise InvalidInputError("MCP stdio requires an approved byte stream pair")
        if not 0.01 <= request_timeout_s <= 3600:
            raise InvalidInputError("MCP deadline is outside the allowed bounds")
        self.reader = reader
        self.writer = writer
        self.timeout = request_timeout_s
        self._lock = asyncio.Lock()
        self._poisoned = False

    async def _send(self, message: dict[str, Any]) -> None:
        self.writer.write(encoded_frame(message) + b"\n")
        await self.writer.drain()

    async def exchange(
        self,
        message: dict[str, Any],
        expect_reply: bool,
    ) -> dict[str, Any] | None:
        if "method" not in message or not isinstance(message["method"], str):
            raise InvalidInputError("MCP stdio request has no method")
        request_id = message.get("id")
        if expect_reply and (type(request_id) is not int or request_id <= 0):
            raise InvalidInputError("MCP stdio request ID must be a positive integer")
        if not expect_reply and "id" in message:
            raise InvalidInputError("MCP stdio notification must not have a request ID")
        async with self._lock:
            if self._poisoned:
                raise IndeterminateExecutionError(
                    "MCP stdio stream is not reusable after an uncertain call"
                )
            try:
                async with asyncio.timeout(self.timeout):
                    await self._send(message)
                    if not expect_reply:
                        return None
                    for _ in range(MAX_NOTIFICATION_MESSAGES + 1):
                        try:
                            line = await self.reader.readline()
                        except ValueError as error:
                            raise McpProtocolError("Oversized MCP stdio line") from error
                        if not line:
                            raise IndeterminateExecutionError("MCP child exited before reply")
                        if len(line) > MAX_WIRE_BYTES + 1 or not line.endswith(b"\n"):
                            raise McpProtocolError("Invalid MCP stdio frame boundary")
                        document = parse_wire(line[:-1])
                        if "id" not in document and isinstance(document.get("method"), str):
                            if not document["method"].startswith("notifications/"):
                                raise McpProtocolError("Unexpected MCP peer notification method")
                            continue
                        if "method" in document and "id" in document:
                            # We deliberately do not implement server-triggered
                            # sampling/elicitation/roots in this increment.
                            await self._send(
                                {
                                    "jsonrpc": "2.0",
                                    "id": document["id"],
                                    "error": {
                                        "code": -32601,
                                        "message": "Client method unavailable",
                                    },
                                }
                            )
                            continue
                        if document.get("id") != request_id:
                            raise McpProtocolError("MCP child sent a response for another request")
                        return document
                    raise McpProtocolError("MCP peer sent too many unsolicited messages")
            except TimeoutError as error:
                self._poisoned = True
                raise IndeterminateExecutionError(
                    "MCP request deadline exceeded; do not replay writes"
                ) from error
            except (BrokenPipeError, ConnectionError, OSError) as error:
                self._poisoned = True
                raise IndeterminateExecutionError(
                    "MCP stdio connection failed; dispatch outcome unknown"
                ) from error
            except BaseException:
                # Includes cancellation: an already-sent request may complete
                # remotely. Never use this stream for another request.
                self._poisoned = True
                raise

    async def close(self) -> None:
        self._poisoned = True
        self.writer.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), timeout=1.0)
        except (OSError, ConnectionError, TimeoutError):
            # The admitted process supervisor is responsible for terminating
            # the child; closing pipes is not proof that it exited.
            pass
