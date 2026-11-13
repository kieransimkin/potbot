"""Admitted HTTP tool exchanges over a monitored worker's existing pipes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from textwrap import indent
from typing import Any

from potbot.domain.errors import IndeterminateExecutionError, ScopeDeniedError
from potbot.domain.serialization import content_hash
from potbot.tools.mcp_protocol import TOOLS_LISTEN, Exchange, decode_reply

# Run from the pinned Python interpreter in the worker image. The definition
# fingerprint covers these exact arguments. Only the controller opens HTTP.
_WORKER_PREFIX = ("-I", "-B", "-c")
_PREAMBLE = """import hashlib
import json
import re
import sys

def receive(method):
    line = sys.stdin.buffer.readline(2 * 1024 * 1024 + 2)
    if not line.endswith(b"\\n") or len(line) > 2 * 1024 * 1024 + 1:
        raise SystemExit(1)
    value = json.loads(line)
    if value["jsonrpc"] != "2.0" or value["method"] != method:
        raise SystemExit(1)
    return value

def acknowledge(request, digest):
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                      "result": {"sha256": digest}}), flush=True)

"""
_EXCHANGE = """call = receive("tools/call")
canonical = json.dumps(call, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False).encode()
acknowledge(call, hashlib.sha256(canonical).hexdigest())
result = receive("potbot/receipt")
digest = result["params"]["sha256"]
if result["id"] != call["id"] or re.fullmatch("[0-9a-f]{64}", digest) is None:
    raise SystemExit(1)
acknowledge(result, digest)
"""
_EOF = """if sys.stdin.buffer.read(1):
    raise SystemExit(1)
"""


# Preserve the original relay bytes and their installation fingerprint.
WORKER_ARGS = (*_WORKER_PREFIX, _PREAMBLE + _EXCHANGE + _EOF)
CONFIRMATION_WORKER_ARGS = (
    *_WORKER_PREFIX,
    _PREAMBLE + _EXCHANGE + "if sys.stdin.buffer.peek(1):\n" + indent(_EXCHANGE, "    ") + _EOF,
)


SUBSCRIPTION_WORKER_ARGS = (
    *_WORKER_PREFIX,
    _PREAMBLE
    + _EXCHANGE.replace("tools/call", TOOLS_LISTEN)
    + "if sys.stdin.buffer.peek(1):\n"
    + indent(_EXCHANGE, "    ")
    + _EOF,
)


def check_http_worker(
    argv: tuple[str, ...], *, http: bool, confirmation: bool, watch_ms: int = 0
) -> None:
    if type(watch_ms) is not int or (watch_ms and (confirmation or not 250 <= watch_ms <= 5000)):
        raise ScopeDeniedError("MCP catalogue watch requires a pinned subscription-only profile")
    worker = WORKER_ARGS
    if confirmation:
        worker = CONFIRMATION_WORKER_ARGS
    if watch_ms:
        worker = SUBSCRIPTION_WORKER_ARGS
    if (http and argv != worker) or (
        not http
        and (
            confirmation
            or watch_ms
            or argv in (WORKER_ARGS, CONFIRMATION_WORKER_ARGS, SUBSCRIPTION_WORKER_ARGS)
        )
    ):
        raise ScopeDeniedError("MCP HTTP requires its pinned relay and provider")


async def relay_http_call(
    request: dict[str, Any],
    worker: Exchange,
    broker: Exchange,
    check_live: Callable[[], Awaitable[None]],
    *,
    digest_only: bool = False,
) -> dict[str, Any]:
    """Keep request identity and credential use under controller authority."""
    ident = request["id"]
    frame = request
    if digest_only:
        frame = {**request, "params": {"sha256": content_hash(request)}}
    accepted = await worker(frame, True)
    if accepted is None or decode_reply(accepted, ident) != {"sha256": content_hash(frame)}:
        raise IndeterminateExecutionError("MCP worker did not acknowledge the admitted request")
    await check_live()
    pending = asyncio.ensure_future(broker(request, True))
    try:
        while not pending.done():
            await asyncio.wait({pending}, timeout=0.1)
            await check_live()
        reply = pending.result()
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    if reply is None:
        raise IndeterminateExecutionError("MCP broker returned no reply")
    await check_live()
    digest = {"sha256": content_hash(reply)}
    acknowledged = await worker(
        {"jsonrpc": "2.0", "id": ident, "method": "potbot/receipt", "params": digest}, True
    )
    if acknowledged is None or decode_reply(acknowledged, ident) != digest:
        raise IndeterminateExecutionError("MCP worker did not acknowledge the broker receipt")
    return reply
