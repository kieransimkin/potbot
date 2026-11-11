"""Bounded signed delivery and result collection through an installed Syn route."""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

from potbot.context.syn_context_route import SynRoundTrip, dispatch_id
from potbot.domain.errors import ScopeDeniedError

_ACCEPTED = b"potbot.rpc.accepted/1"


@dataclass(slots=True)
class _PendingCall:
    payload: bytes
    received: bytes | None = None
    reply: bytes | None = None


class DirectedRpcCall:
    def __init__(self, route: DirectedRpcRoute, ident: UUID, pending: _PendingCall) -> None:
        self._route, self._ident, self._pending = route, ident, pending

    def receive(self) -> bytes:
        route, pending = self._route, self._pending
        route._verify(self._ident, pending.payload, _ACCEPTED)
        with route._lock:
            if route._closed or pending.received is None:
                raise ScopeDeniedError("Syn did not deliver the admitted request")
            return pending.received

    def collect(self, reply: bytes) -> bytes:
        route, pending = self._route, self._pending
        if not 1 <= len(reply) <= route.max_reply:
            raise ScopeDeniedError("Directed RPC result exceeds its bound")
        collection = uuid4()
        with route._lock:
            if route._closed or pending.received is None or pending.reply is not None:
                raise ScopeDeniedError("Syn route cannot collect this result")
            pending.reply = reply
            route._collections[collection] = self._ident
        try:
            signed_reply = route._verify(collection, self._ident.bytes, reply)
            with route._lock:
                if route._closed or collection in route._collections:
                    raise ScopeDeniedError("Syn did not collect the admitted result")
            return signed_reply
        finally:
            with route._lock:
                route._collections.pop(collection, None)


class DirectedRpcRoute:
    def __init__(
        self,
        workspace_id: UUID,
        boot_id: str,
        uid: int,
        exchange: SynRoundTrip,
        *,
        method: str,
        max_request: int,
        max_reply: int,
    ) -> None:
        self.workspace_id, self.boot_id, self.uid = workspace_id, boot_id, uid
        self.method, self.max_request, self.max_reply = method, max_request, max_reply
        self._exchange = exchange
        self._lock = threading.Lock()
        self._deliveries: dict[UUID, _PendingCall] = {}
        self._collections: dict[UUID, UUID] = {}
        self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True

    @contextmanager
    def pending(self, payload: bytes) -> Iterator[DirectedRpcCall]:
        if not 1 <= len(payload) <= self.max_request:
            raise ScopeDeniedError("Directed RPC request exceeds its bound")
        ident, slot = uuid4(), _PendingCall(payload)
        with self._lock:
            if self._closed or len(self._deliveries) >= 128:
                raise ScopeDeniedError("Syn route is unavailable")
            self._deliveries[ident] = slot
        try:
            yield DirectedRpcCall(self, ident, slot)
        finally:
            with self._lock:
                self._deliveries.pop(ident, None)

    def _verify(self, ident: UUID, payload: bytes, expected: bytes) -> bytes:
        reply, boot = self._exchange(ident, payload)
        if type(reply) is not bytes or reply != expected or boot != self.boot_id:
            raise ScopeDeniedError("Syn reply differs from the admitted exchange")
        return reply

    def accept(self, header: Mapping[str, object], payload: bytes) -> bytes:
        ident = dispatch_id(
            header,
            payload,
            workspace_id=self.workspace_id,
            method=self.method,
            operator_boot=self.boot_id,
            caller_uid=self.uid,
            max_payload=self.max_request,
        )
        with self._lock:
            if self._closed:
                raise ScopeDeniedError("Syn route is closed")
            delivery = self._collections.get(ident)
            if delivery is not None:
                slot = self._deliveries.get(delivery)
                if payload != delivery.bytes or slot is None or slot.reply is None:
                    raise ScopeDeniedError("Syn collection does not match the admitted delivery")
                del self._collections[ident]
                return slot.reply
            pending = self._deliveries.get(ident)
            if pending is None or pending.received is not None or payload != pending.payload:
                raise ScopeDeniedError("Syn delivery has no admitted request")
            pending.received = payload
            return _ACCEPTED
