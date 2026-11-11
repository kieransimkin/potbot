"""Bounded worker frames with an explicitly selected Context transport.

Only a host-selected handler has identity. No listener, socket pathname, token,
SQL credential, shell command, or arbitrary filesystem path is exposed.
"""

from __future__ import annotations

import asyncio
import json
import math
import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from potbot.context.rpc_contract import (
    MAX_CHANNEL_BYTES,
    MAX_CHANNEL_REQUESTS,
    MAX_FRAME_BYTES,
    SCHEMA,
    OperationRequest,
    parse_request,
)
from potbot.domain.errors import DomainError, InvalidInputError

RPC_TRANSPORT = "stdio"

_MESSAGES = {
    "invalid_input": "Invalid context request",
    "scope_denied": "Context operation not authorized",
    "revision_conflict": "Context revision or request ID conflicts; read before retrying",
    "state_mismatch": "Context operation is fenced",
    "timeout": "Context operation timed out",
    "source_unavailable": "Context service unavailable",
    "capability_unavailable": "Context capability unavailable",
    "indeterminate": "Context outcome needs reconciliation; do not invent a new request ID",
}


@dataclass(frozen=True, slots=True)
class ChannelResult:
    requests: int
    errors: int
    received_bytes: int


def _response_payload(value: dict[str, Any]) -> bytes:
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if not 1 <= len(payload) <= MAX_FRAME_BYTES:
        raise InvalidInputError("Context response exceeds its frame bound")
    return payload


def response_bytes(value: dict[str, Any]) -> bytes:
    return _frame(_response_payload(value))


def _frame(payload: bytes) -> bytes:
    if type(payload) is not bytes or not 1 <= len(payload) <= MAX_FRAME_BYTES:
        raise InvalidInputError("Context response exceeds its frame bound")
    return struct.pack("!I", len(payload)) + payload


def evaluate_context_frame(
    payload: bytes, handler: Callable[[OperationRequest], dict[str, Any]]
) -> bytes:
    """Execute at the selected Context endpoint and return the exact reply payload."""
    request: OperationRequest | None = None
    try:
        request = parse_request(payload)
        response = {
            "schema": SCHEMA,
            "request_id": str(request.request_id),
            "ok": True,
            "result": handler(request),
        }
    except (TimeoutError, ConnectionError, OSError):
        raise
    except DomainError as exc:
        code = exc.code.value
        response = _error_response(
            request,
            code if code in _MESSAGES else "internal_error",
            _MESSAGES.get(code, "Context operation unavailable"),
        )
    except Exception:
        response = _error_response(request, "internal_error", "Context operation unavailable")
    try:
        return _response_payload(response)
    except (InvalidInputError, ValueError, UnicodeError, TypeError):
        return _response_payload(
            _error_response(request, "invalid_input", "Context response exceeds its bound")
        )


def _error_response(request: OperationRequest | None, code: str, message: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "request_id": str(request.request_id) if request else None,
        "ok": False,
        "error": {"code": code, "message": message},
    }


async def _read(reader: asyncio.StreamReader) -> bytes | None:
    try:
        prefix = await reader.readexactly(4)
    except asyncio.IncompleteReadError as exc:
        if exc.partial:
            raise InvalidInputError("Truncated context frame") from None
        return None
    length = struct.unpack("!I", prefix)[0]
    if not 1 <= length <= MAX_FRAME_BYTES:
        raise InvalidInputError("Invalid context frame size")
    try:
        return await reader.readexactly(length)
    except asyncio.IncompleteReadError:
        raise InvalidInputError("Truncated context frame") from None


async def _settled_call(forward: Callable[[bytes], bytes], payload: bytes) -> bytes:
    # Cancellation cannot undo a synchronous PostgreSQL commit. Keep a strong
    # reference and wait for the bounded DB operation before revoking/closing.
    task = asyncio.create_task(asyncio.to_thread(forward, payload))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Repeated cancellation must not detach an outstanding DB mutation.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()  # retrieve any failure; the original cancellation wins
        raise


async def serve_context_streams(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    handler: Callable[[OperationRequest], dict[str, Any]] | None = None,
    *,
    forward: Callable[[bytes], bytes] | None = None,
    frame_timeout: float = 15.0,
) -> ChannelResult:
    """Serve ONE already-admitted channel; caller must revoke its session in finally.

    Stdout is reserved for protocol frames, stderr for worker logs. Request/frame
    bounds apply even when the same valid request is replayed repeatedly.
    """
    if (
        isinstance(frame_timeout, bool)
        or not isinstance(frame_timeout, int | float)
        or not math.isfinite(frame_timeout)
        or not 0 < frame_timeout <= 60
    ):
        raise InvalidInputError("Invalid context channel deadline")
    if (handler is None) == (forward is None):
        raise InvalidInputError("Select exactly one Context channel route")
    if forward is None:
        assert handler is not None
        local = handler

        def local_forward(payload: bytes) -> bytes:
            return evaluate_context_frame(payload, local)

        forward = local_forward

    count = errors = received = sent = 0
    try:
        while count < MAX_CHANNEL_REQUESTS:
            try:
                async with asyncio.timeout(frame_timeout):
                    payload = await _read(reader)
                if payload is None:
                    break
                received += len(payload) + 4
                if received > MAX_CHANNEL_BYTES:
                    raise InvalidInputError("Context channel byte budget exhausted")
            except (TimeoutError, ConnectionError, OSError):
                break
            except InvalidInputError:
                reply = _response_payload(
                    _error_response(None, "invalid_input", "Invalid context request")
                )
            else:
                try:
                    reply = await _settled_call(forward, payload)
                except (TimeoutError, ConnectionError, OSError):
                    break
            wire = _frame(reply)
            response = json.loads(reply)
            request_id = response["request_id"]
            count += request_id is not None
            success = response["ok"] is True
            if not success:
                errors += 1
            fatal = not success and (
                request_id is None
                or response["error"]["code"] not in {"revision_conflict", "invalid_input"}
                or response["error"]["message"] == "Context response exceeds its bound"
            )
            sent += len(wire)
            if sent > MAX_CHANNEL_BYTES:
                break
            writer.write(wire)
            try:
                async with asyncio.timeout(frame_timeout):
                    await writer.drain()
            except (TimeoutError, ConnectionError, OSError):
                break  # A committed write may have no delivered reply. Receipt remains durable.
            if fatal:
                break
    finally:
        writer.close()
        try:
            async with asyncio.timeout(1.0):
                await writer.wait_closed()
        except (TimeoutError, ConnectionError, OSError):
            pass
    return ChannelResult(count, errors, received)
