"""One admitted HTTP tool exchange over a monitored worker's existing pipes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from potbot.domain.errors import IndeterminateExecutionError
from potbot.domain.serialization import content_hash
from potbot.tools.mcp_protocol import Exchange, decode_reply

# Run from the pinned Python interpreter in the worker image. The definition
# fingerprint covers these exact arguments. Only the controller opens HTTP.
WORKER_ARGS = (
    "-I",
    "-B",
    "-c",
    """import hashlib
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

call = receive("tools/call")
canonical = json.dumps(call, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False).encode()
acknowledge(call, hashlib.sha256(canonical).hexdigest())
result = receive("potbot/receipt")
digest = result["params"]["sha256"]
if result["id"] != call["id"] or re.fullmatch("[0-9a-f]{64}", digest) is None:
    raise SystemExit(1)
acknowledge(result, digest)
if sys.stdin.buffer.read(1):
    raise SystemExit(1)
""",
)


async def relay_http_call(
    request: dict[str, Any],
    worker: Exchange,
    broker: Exchange,
    check_live: Callable[[], Awaitable[None]],
) -> dict[str, Any]:
    """Keep request identity and credential use under controller authority."""
    ident = request["id"]
    accepted = await worker(request, True)
    if accepted is None or decode_reply(accepted, ident) != {"sha256": content_hash(request)}:
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
