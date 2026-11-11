"""Directed browser commands for a workspace with a pinned Syn operator."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from potbot.context.installed_syn_route import (
    BROWSER_METHOD,
    installed_directed_rpc,
    serve_directed_rpc,
)
from potbot.context.syn_context_route import SynRoundTrip, serve_rpc_connection
from potbot.context.syn_directed_rpc import DirectedRpcRoute
from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.tools.xpra_browser_control import MAX_REPLY, MAX_REQUEST
from potbot.tools.xpra_browser_worker import validate_command

_REJECTED = b'{"ok":false}'


class SynBrowserRoute(DirectedRpcRoute):
    def __init__(self, workspace_id: UUID, boot_id: str, uid: int, exchange: SynRoundTrip) -> None:
        super().__init__(
            workspace_id,
            boot_id,
            uid,
            exchange,
            method=BROWSER_METHOD,
            max_request=MAX_REQUEST,
            max_reply=MAX_REPLY,
        )

    def call(
        self,
        command: object,
        execute: Callable[[object], dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            payload = json.dumps(
                validate_command(command), separators=(",", ":"), allow_nan=False
            ).encode("ascii")
        except (ValueError, TypeError) as exc:
            raise InvalidInputError("invalid controlled-browser operation") from exc
        # Leave room for the worker's existing bounded process envelope.
        if len(payload) > MAX_REQUEST - len(b'{"id":2147483648,"command":}\n'):
            raise InvalidInputError("browser operation is too large")
        try:
            with self.pending(payload) as pending:
                received = pending.receive()
                # The browser reservation remains held through both signed exchanges.
                try:
                    result = execute(json.loads(received))
                except InvalidInputError:
                    reply = _REJECTED
                else:
                    reply = json.dumps(
                        {"ok": True, "result": result},
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("ascii")
                reply = pending.collect(reply)
        except Exception as exc:
            raise StateMismatchError(
                "Syn browser exchange is indeterminate; session fenced"
            ) from exc
        if reply == _REJECTED:
            raise InvalidInputError("browser command was rejected or failed")
        return cast(dict[str, Any], json.loads(reply)["result"])


@contextmanager
def installed_browser_route(
    selection: dict[str, Any],
    workspace_id: UUID,
    *,
    protected_mounts: tuple[Path, ...],
) -> Iterator[SynBrowserRoute]:
    endpoint, boot, exchange = installed_directed_rpc(
        selection,
        workspace_id,
        protected_mounts=protected_mounts,
        method=BROWSER_METHOD,
        max_reply=MAX_REPLY,
    )
    uid = os.getuid()
    route = SynBrowserRoute(workspace_id, boot, uid, exchange)
    with serve_directed_rpc(
        endpoint,
        lambda conn: serve_rpc_connection(
            conn, operator_uid=uid, max_payload=MAX_REQUEST, accept=route.accept
        ),
    ):
        try:
            yield route
        finally:
            route.close()
