"""Bounded DAP framing over already admitted pipes; never starts a process.

The controller retains process/lease/budget and cleanup ownership. Timeouts,
cancellation and EOF poison the connection; effectful requests are never replayed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from potbot.debugging.contracts import EventHandler
from potbot.domain.errors import (
    CapabilityUnavailableError,
    IndeterminateExecutionError,
    InvalidInputError,
    ToolFailureError,
)
from potbot.domain.serialization import canonical_json

MAX_FRAME_BYTES = 1024 * 1024
MAX_HEADER_BYTES = 8192
MAX_PENDING = 16
_COMMANDS = {
    "initialize": "initialize",
    "launch": "launch",
    "attach": "attach",
    "set_breakpoints": "setBreakpoints",
    "configure_exceptions": "setExceptionBreakpoints",
    "configuration_done": "configurationDone",
    "continue": "continue",
    "pause": "pause",
    "step_over": "next",
    "step_into": "stepIn",
    "step_out": "stepOut",
    "threads": "threads",
    "stack": "stackTrace",
    "scopes": "scopes",
    "variables": "variables",
    "evaluate": "evaluate",
    "exception_info": "exceptionInfo",
    "disconnect": "disconnect",
    "terminate": "terminate",
}
_KEYS = {
    "thread_id": "threadId",
    "frame_id": "frameId",
    "reference": "variablesReference",
    "start_frame": "startFrame",
    "terminate_debuggee": "terminateDebuggee",
}


def _frame(message: Mapping[str, Any]) -> bytes:
    raw = canonical_json(dict(message)).encode("utf-8")
    if len(raw) > MAX_FRAME_BYTES:
        raise InvalidInputError("Debugger wire message exceeds its byte budget")
    return f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii") + raw


class DapAdapter:
    """Asynchronous response/event demultiplexing with no executable authority."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        timeout_s: float = 30.0,
        max_messages: int = 100_000,
    ) -> None:
        if not 0.01 <= timeout_s <= 300 or not 1 <= max_messages <= 100_000:
            raise InvalidInputError("Debugger protocol limits are invalid")
        self.reader, self.writer, self.timeout = reader, writer, timeout_s
        self.on_event: EventHandler | None = None
        self._seq = 0
        self._pending: dict[int, tuple[str, asyncio.Future[dict[str, Any]]]] = {}
        self._failed = False
        self.failure_reason: str | None = None
        self._send_lock = asyncio.Lock()
        self._max_messages = max_messages
        self._pump = asyncio.create_task(self._read_loop(), name="potbot-dap-reader")

    async def _write(self, message: Mapping[str, Any]) -> None:
        async with self._send_lock:
            self.writer.write(_frame(message))
            await self.writer.drain()

    async def begin(self, operation: str, arguments: Mapping[str, Any]) -> int:
        if operation not in _COMMANDS:
            raise CapabilityUnavailableError("Unsupported debugger operation")
        if self._failed:
            raise IndeterminateExecutionError("Debugger connection is no longer reusable")
        if len(self._pending) >= MAX_PENDING:
            raise InvalidInputError("Too many outstanding debugger requests")
        payload: dict[str, Any] = {_KEYS.get(k, k): v for k, v in arguments.items()}
        if operation == "initialize":
            payload = {
                "clientID": arguments["client_name"],
                "adapterID": arguments["adapter_id"],
                "pathFormat": "path",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "supportsVariablePaging": True,
                "supportsRunInTerminalRequest": False,
            }
        if operation == "set_breakpoints":
            payload = {
                "source": {"path": arguments["source_path"]},
                "breakpoints": [{"line": line} for line in arguments["lines"]],
            }
        self._seq += 1
        sequence = self._seq
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[sequence] = (_COMMANDS[operation], future)
        try:
            async with asyncio.timeout(self.timeout):
                await self._write(
                    {
                        "seq": sequence,
                        "type": "request",
                        "command": _COMMANDS[operation],
                        "arguments": payload,
                    }
                )
        except BaseException:
            self._poison()
            raise
        return sequence

    async def finish(self, request_id: int) -> dict[str, Any]:
        entry = self._pending.get(request_id)
        if entry is None:
            raise InvalidInputError("Unknown debugger request handle")
        try:
            async with asyncio.timeout(self.timeout):
                result = await asyncio.shield(entry[1])
            return self._normalize(entry[0], result)
        except (TimeoutError, asyncio.CancelledError) as error:
            self._poison()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise IndeterminateExecutionError("Debugger request timed out; do not replay") from None
        finally:
            self._pending.pop(request_id, None)

    async def call(self, operation: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return await self.finish(await self.begin(operation, arguments))

    async def _read_message(self) -> dict[str, Any]:
        header = await self.reader.readuntil(b"\r\n\r\n")
        if len(header) > MAX_HEADER_BYTES:
            raise ValueError("header budget")
        lengths = []
        for line in header[:-4].split(b"\r\n"):
            key, value = line.split(b":", 1)
            if key.lower() == b"content-length":
                lengths.append(value.strip())
        if len(lengths) != 1 or not lengths[0].isdigit():
            raise ValueError("invalid Content-Length")
        length = int(lengths[0])
        if not 1 <= length <= MAX_FRAME_BYTES:
            raise ValueError("message budget")
        message = json.loads((await self.reader.readexactly(length)).decode("utf-8"))
        if not isinstance(message, dict) or type(message.get("seq")) is not int:
            raise ValueError("invalid message")
        canonical_json(message).encode("utf-8")
        return message

    async def _read_loop(self) -> None:
        try:
            for _ in range(self._max_messages):
                message = await self._read_message()
                kind = message.get("type")
                if kind == "response":
                    request_seq = message.get("request_seq")
                    if type(request_seq) is not int:
                        raise ValueError("invalid request correlation")
                    entry = self._pending.get(request_seq)
                    if entry is None or message.get("command") != entry[0] or entry[1].done():
                        raise ValueError("uncorrelated response")
                    if type(message.get("success")) is not bool:
                        raise ValueError("invalid response status")
                    body = message.get("body", {})
                    if not isinstance(body, dict):
                        raise ValueError("invalid response body")
                    if message["success"]:
                        entry[1].set_result(body)
                    else:
                        # Peer error text may contain locals, paths or credentials.
                        entry[1].set_exception(ToolFailureError("Debugger rejected the operation"))
                elif kind == "event":
                    event, body = message.get("event"), message.get("body", {})
                    if not isinstance(event, str) or not isinstance(body, dict):
                        raise ValueError("invalid event")
                    if self.on_event is not None:
                        self.on_event(
                            event,
                            {
                                {
                                    "threadId": "thread_id",
                                    "allThreadsStopped": "all_threads_stopped",
                                    "systemProcessId": "process_id",
                                    "exitCode": "exit_code",
                                }.get(str(k), str(k)): v
                                for k, v in body.items()
                            },
                        )
                elif kind == "request":
                    # Never spawn terminals/children or accept debugger-originated authority.
                    self._seq += 1
                    await self._write(
                        {
                            "seq": self._seq,
                            "type": "response",
                            "request_seq": message["seq"],
                            "command": message.get("command", ""),
                            "success": False,
                            "message": "Reverse requests are not admitted",
                        }
                    )
                else:
                    raise ValueError("unsupported message kind")
            raise ValueError("session message budget exhausted")
        except asyncio.CancelledError:
            return
        except Exception as error:
            self.failure_reason = type(error).__name__
            self._poison()

    def _poison(self) -> None:
        if self._failed:
            return
        self._failed = True
        for _, future in self._pending.values():
            if not future.done():
                future.set_exception(IndeterminateExecutionError("Debugger connection lost"))
                # begin/finish can be separated by configuration; consume abandoned errors.
                future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        if self.on_event is not None:
            self.on_event("connection_lost", {})
        self.writer.close()

    @staticmethod
    def _normalize(command: str, body: dict[str, Any]) -> dict[str, Any]:
        if command == "initialize":
            supported = set(_COMMANDS) - {"terminate", "exception_info", "configuration_done"}
            for flag, operation in (
                ("supportsTerminateRequest", "terminate"),
                ("supportsExceptionInfoRequest", "exception_info"),
                ("supportsConfigurationDoneRequest", "configuration_done"),
            ):
                if body.get(flag) is True:
                    supported.add(operation)
            return {"operations": sorted(supported), "adapter_capabilities": body}
        if command == "stackTrace":
            return {
                "frames": [
                    {
                        "id": f["id"],
                        "name": f.get("name", ""),
                        "source": (f.get("source") or {}).get("path"),
                        "line": f.get("line", 0),
                        "column": f.get("column", 0),
                    }
                    for f in body.get("stackFrames", [])
                ]
            }
        if command == "scopes":
            return {
                "scopes": [
                    {
                        "name": s["name"],
                        "reference": s["variablesReference"],
                        "expensive": s.get("expensive", False),
                    }
                    for s in body.get("scopes", [])
                ]
            }
        if command == "variables":
            return {
                "variables": [
                    {
                        "name": v["name"],
                        "value": v["value"],
                        "type": v.get("type"),
                        "reference": v.get("variablesReference", 0),
                    }
                    for v in body.get("variables", [])
                ]
            }
        return body

    async def close(self) -> None:
        self.on_event = None
        self._poison()
        self._pump.cancel()
        await asyncio.gather(self._pump, return_exceptions=True)
        try:
            await asyncio.wait_for(self.writer.wait_closed(), timeout=1)
        except (OSError, TimeoutError):
            pass
