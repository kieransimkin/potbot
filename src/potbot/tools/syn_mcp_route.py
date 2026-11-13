"""Directed internal dispatch for sandboxed read-only MCP tool calls."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.context.installed_syn_route import (
    MCP_METHOD,
    installed_directed_rpc,
    serve_directed_rpc,
)
from potbot.context.syn_context_route import SynRoundTrip, serve_rpc_connection
from potbot.context.syn_directed_rpc import DirectedRpcRoute
from potbot.domain.errors import IndeterminateExecutionError, ScopeDeniedError
from potbot.tools.mcp_protocol import (
    MAX_WIRE_BYTES,
    TOOLS_LISTEN,
    Exchange,
    encoded_frame,
    parse_wire,
)


class SynMcpRoute(DirectedRpcRoute):
    def __init__(self, workspace_id: UUID, boot_id: str, uid: int, exchange: SynRoundTrip) -> None:
        super().__init__(
            workspace_id,
            boot_id,
            uid,
            exchange,
            method=MCP_METHOD,
            max_request=MAX_WIRE_BYTES,
            max_reply=MAX_WIRE_BYTES,
        )

    async def invoke(self, request: dict[str, Any], dispatch: Exchange) -> dict[str, Any]:
        if (
            request.get("jsonrpc") != "2.0"
            or request.get("method") not in ("tools/call", TOOLS_LISTEN)
            or type(request.get("id")) is not int
            or request["id"] <= 0
        ):
            raise ScopeDeniedError("Syn MCP route accepts only admitted tool requests")
        with self.pending(encoded_frame(request)) as pending:
            try:
                payload = await asyncio.to_thread(pending.receive)
            except Exception as exc:
                raise IndeterminateExecutionError(
                    "Syn did not confirm the MCP tool request"
                ) from exc

            reply = await dispatch(parse_wire(payload), True)
            if reply is None:
                raise IndeterminateExecutionError("Sandboxed MCP tool returned no reply")
            try:
                signed = await asyncio.to_thread(pending.collect, encoded_frame(reply))
            except Exception as exc:
                raise IndeterminateExecutionError(
                    "Syn did not confirm the MCP tool result"
                ) from exc
            return parse_wire(signed)


@contextmanager
def installed_mcp_route(
    selection: dict[str, Any],
    workspace_id: UUID,
    *,
    protected_mounts: tuple[Path, ...],
) -> Iterator[SynMcpRoute]:
    endpoint, boot, exchange = installed_directed_rpc(
        selection,
        workspace_id,
        protected_mounts=protected_mounts,
        method=MCP_METHOD,
        max_reply=MAX_WIRE_BYTES,
    )
    uid = os.getuid()
    route = SynMcpRoute(workspace_id, boot, uid, exchange)
    with serve_directed_rpc(
        endpoint,
        lambda conn: serve_rpc_connection(
            conn, operator_uid=uid, max_payload=MAX_WIRE_BYTES, accept=route.accept
        ),
    ):
        try:
            yield route
        finally:
            route.close()
