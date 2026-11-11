"""Directed browser commands for a workspace with a pinned Syn operator."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from potbot.context.installed_syn_route import (
    BROWSER_METHOD,
    installed_directed_rpc,
    serve_directed_rpc,
)
from potbot.context.syn_context_route import SynRoundTrip, dispatch_id, serve_rpc_connection
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools.xpra_browser_control import MAX_REPLY, MAX_REQUEST
from potbot.tools.xpra_browser_worker import validate_command

_ACCEPTED = b"potbot.browser.accepted/1"
_REJECTED = b'{"ok":false}'


@dataclass
class _Command:
    payload: bytes
    received: bytes | None = None
    reply: bytes | None = None


class SynBrowserRoute:
    def __init__(self, workspace_id: UUID, boot_id: str, uid: int, exchange: SynRoundTrip) -> None:
        self.workspace_id = workspace_id
        self.boot_id = boot_id
        self.uid = uid
        self.exchange = exchange
        self._lock = threading.Lock()
        self._deliveries: dict[UUID, _Command] = {}
        self._collections: dict[UUID, UUID] = {}
        self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True

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
        delivery, collection = uuid4(), uuid4()
        slot = _Command(payload)
        with self._lock:
            if self._closed or len(self._deliveries) >= 128:
                raise ScopeDeniedError("Syn browser route is unavailable")
            self._deliveries[delivery] = slot
        try:
            self._verify(delivery, payload, _ACCEPTED)
            with self._lock:
                if self._closed or slot.received is None:
                    raise ScopeDeniedError("Syn did not deliver the browser command")
                received = slot.received
            # The caller holds the exclusive browser reservation until both
            # signed exchanges finish. Browser work has its own deadline.
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
            if len(reply) > MAX_REPLY:
                raise StateMismatchError("browser result exceeds its bound")
            with self._lock:
                if self._closed:
                    raise ScopeDeniedError("Syn browser route stopped before result collection")
                slot.reply = reply
                self._collections[collection] = delivery
            self._verify(collection, delivery.bytes, reply)
            with self._lock:
                if self._closed or collection in self._collections:
                    raise ScopeDeniedError("Syn did not collect the browser result")
        except Exception as exc:
            raise StateMismatchError(
                "Syn browser exchange is indeterminate; session fenced"
            ) from exc
        finally:
            with self._lock:
                self._deliveries.pop(delivery, None)
                self._collections.pop(collection, None)
        if reply == _REJECTED:
            raise InvalidInputError("browser command was rejected or failed")
        return cast(dict[str, Any], json.loads(reply)["result"])

    def _verify(self, ident: UUID, payload: bytes, expected: bytes) -> None:
        reply, boot = self.exchange(ident, payload)
        if reply != expected or boot != self.boot_id:
            raise ScopeDeniedError("Syn browser reply differs from the selected operation")

    def accept(self, header: Mapping[str, object], payload: bytes) -> bytes:
        ident = dispatch_id(
            header,
            payload,
            workspace_id=self.workspace_id,
            method=BROWSER_METHOD,
            operator_boot=self.boot_id,
            caller_uid=self.uid,
            max_payload=MAX_REQUEST,
        )
        with self._lock:
            if self._closed:
                raise ScopeDeniedError("Syn browser route is closed")
            delivery = self._collections.get(ident)
            if delivery is not None:
                slot = self._deliveries[delivery]
                if payload != delivery.bytes or slot.reply is None:
                    raise ScopeDeniedError("Syn collection does not match the browser command")
                del self._collections[ident]
                return slot.reply
            pending = self._deliveries.get(ident)
            if pending is None or pending.received is not None or payload != pending.payload:
                raise ScopeDeniedError("Syn delivery has no admitted browser command")
            pending.received = payload
            return _ACCEPTED


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
