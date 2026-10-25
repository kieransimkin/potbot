"""Operator-owned, per-Xpra-session exclusive browser command reservations.

Tokens never appear in agent prompts, SYN values or public inventories. This is
process-local admission: an Xpra supervisor owns one coordinator, and restarts
terminate its browsers. No wall-clock stale lease is silently transferred.
"""
from __future__ import annotations

import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator
from uuid import UUID

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError


@dataclass(frozen=True, slots=True)
class BrowserLease:
    workspace_id: UUID
    session_id: str
    owner_kind: str
    owner_id: UUID
    token: str
    generation: int
    expires_at: float

    def public(self) -> dict[str, object]:
        return {"sessionId": self.session_id, "reserved": True,
                "holder": self.owner_kind, "generation": self.generation,
                "expiresInSeconds": max(0, int(self.expires_at - time.monotonic()))}


@dataclass(slots=True)
class _State:
    lease: BrowserLease
    inflight: int = 0
    fenced: bool = False


class BrowserSessionCoordinator:
    """Exclusive per-session reservations, never equivalent to OS process isolation.

    Human Xpra input requires an independently brokered input policy. This
    controls all machine RPC paths admitted through XpraApplicationSupervisor.
    """

    def __init__(self, workspace_id: UUID, principal_id: UUID, *,
                 clock: Callable[[], float] = time.monotonic, maximum: int = 12) -> None:
        if not isinstance(workspace_id, UUID) or not isinstance(principal_id, UUID):
            raise InvalidInputError("browser reservations need operator workspace identity")
        if not 1 <= maximum <= 32 or not callable(clock):
            raise InvalidInputError("invalid browser reservation configuration")
        self.workspace_id, self.principal_id = workspace_id, principal_id
        self._clock = clock
        self._max = maximum
        self._lock = threading.RLock()
        self._slots: dict[str, _State] = {}
        self._generation: dict[str, int] = {}
        # Remember the authenticated holder after a voluntary release. A live
        # Chromium profile can contain its cookies, DOM and browsing history.
        # Another agent must start a NEW browser, even after the old token is released.
        self._last_holder: dict[str, tuple[str, UUID]] = {}

    def _session(self, session: str) -> None:
        import re
        if not isinstance(session, str) or re.fullmatch(r"[0-9a-f]{32}", session) is None:
            raise InvalidInputError("invalid controlled browser session ID")

    def _live(self, state: _State) -> None:
        if state.fenced or self._clock() >= state.lease.expires_at:
            state.fenced = True
            raise StateMismatchError("browser reservation expired or fenced; terminate/reconcile")

    def reserve(self, session: str, *, owner_kind: str, owner_id: UUID,
                ttl_seconds: int = 60) -> BrowserLease:
        self._session(session)
        if owner_kind not in {"operator", "agent"} or not isinstance(owner_id, UUID) or (
            type(ttl_seconds) is not int or not 20 <= ttl_seconds <= 300
        ):
            raise ScopeDeniedError("browser reservation requires operator-approved identity and TTL")
        if owner_kind == "operator" and owner_id != self.principal_id:
            raise ScopeDeniedError("operator browser reservation belongs to another principal")
        with self._lock:
            if session in self._slots:
                # Even an expired reservation is NEVER silently stolen: the
                # prior browser may have run a click after the controller lost contact.
                self._live(self._slots[session])
                raise StateMismatchError("browser is already reserved")
            previous = self._last_holder.get(session)
            if previous is not None and previous != (owner_kind, owner_id):
                raise ScopeDeniedError("browser profile cannot transfer to another owner before termination")
            if len(self._slots) >= self._max:
                raise StateMismatchError("browser reservation capacity reached")
            generation = self._generation.get(session, 0) + 1
            self._generation[session] = generation
            lease = BrowserLease(self.workspace_id, session, owner_kind, owner_id,
                                 secrets.token_urlsafe(32), generation,
                                 self._clock() + ttl_seconds)
            self._slots[session] = _State(lease)
            self._last_holder[session] = (owner_kind, owner_id)
            return lease

    def _require(self, session: str, token: str, owner_kind: str | None = None,
                 owner_id: UUID | None = None) -> _State:
        self._session(session)
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise ScopeDeniedError("browser reservation token unavailable")
        state = self._slots.get(session)
        # Verify exact owner + session + generation token. Never accept a token
        # from a different browser (even in the same workspace).
        if state is None or not secrets.compare_digest(state.lease.token, token) or (
            owner_kind is not None and state.lease.owner_kind != owner_kind
        ) or (owner_id is not None and state.lease.owner_id != owner_id):
            raise ScopeDeniedError("browser reservation owner or token mismatch")
        self._live(state)
        return state

    def renew(self, session: str, token: str, ttl_seconds: int = 60, *,
              owner_kind: str | None = None, owner_id: UUID | None = None) -> BrowserLease:
        if type(ttl_seconds) is not int or not 20 <= ttl_seconds <= 300:
            raise InvalidInputError("invalid browser renewal TTL")
        with self._lock:
            state = self._require(session, token, owner_kind, owner_id)
            old = state.lease
            state.lease = BrowserLease(old.workspace_id, old.session_id, old.owner_kind,
                                       old.owner_id, old.token, old.generation,
                                       self._clock() + ttl_seconds)
            return state.lease

    def release(self, session: str, token: str, *, owner_kind: str | None = None,
                owner_id: UUID | None = None) -> None:
        with self._lock:
            state = self._require(session, token, owner_kind, owner_id)
            if state.inflight:
                raise StateMismatchError("cannot release a browser while its command is in flight")
            del self._slots[session]

    def projection(self, session: str) -> dict[str, object]:
        self._session(session)
        with self._lock:
            state = self._slots.get(session)
            if state is None:
                return {"sessionId": session, "reserved": False, "fenced": False}
            return {**state.lease.public(), "fenced": state.fenced or
                    self._clock() >= state.lease.expires_at, "inflight": state.inflight}

    @contextmanager
    def operation(self, session: str, token: str, *, owner_kind: str | None = None,
                  owner_id: UUID | None = None) -> Iterator[BrowserLease]:
        with self._lock:
            state = self._require(session, token, owner_kind, owner_id)
            if state.inflight:
                raise StateMismatchError("concurrent command on the same browser is denied")
            state.inflight = 1
            lease = state.lease
        try:
            yield lease
        finally:
            with self._lock:
                state.inflight = 0
                if self._clock() >= state.lease.expires_at:
                    state.fenced = True

    def fence(self, session: str) -> None:
        self._session(session)
        with self._lock:
            state = self._slots.get(session)
            if state is not None:
                state.fenced = True

    def retired(self, session: str, *, process_stopped: bool) -> None:
        """Reconcile only after the operator observes actual process termination."""
        self._session(session)
        if process_stopped is not True:
            raise StateMismatchError("cannot clear browser reservation before process stops")
        with self._lock:
            state = self._slots.get(session)
            if state is not None and state.inflight:
                raise StateMismatchError("browser command remains in flight")
            self._slots.pop(session, None)
            self._last_holder.pop(session, None)
