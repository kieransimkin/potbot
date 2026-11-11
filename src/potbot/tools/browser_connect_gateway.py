"""Operator-only, opt-in HTTPS CONNECT egress broker (AF_UNIX only).

This does NOT enable Chromium network access. The ordinary sandbox still has a
private network namespace and denies TCP creation; connecting Chromium to this
broker requires a separately qualified, isolated namespace-local HTTP forwarder.
No host TCP listener, broad fetch API, URL forwarding or DNS rebinding fallback.
"""

from __future__ import annotations

import os
import secrets
import select
import socket
import socketserver
import stat
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools.connect_policy import ConnectPolicy as ConnectPolicy
from potbot.tools.connect_policy import checked_host as checked_host

_MAX_HEADER = 4096
_MAX_TUNNEL = 16 * 1024 * 1024
_MAX_SECONDS = 60


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


class BrowserConnectGateway:
    """Small operator-bound CONNECT relay, never a general forward proxy."""

    def __init__(
        self,
        socket_path: Path,
        *,
        policy: ConnectPolicy,
        token: str,
        resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo,
        connect: Callable[..., socket.socket] = socket.create_connection,
    ) -> None:
        path = Path(socket_path).absolute()
        if (
            not isinstance(policy, ConnectPolicy)
            or not isinstance(token, str)
            or len(token) < 32
            or len(token) > 128
            or "\r" in token
            or "\n" in token
            or path.is_symlink()
            or path.exists()
        ):
            raise ScopeDeniedError("invalid private CONNECT gateway configuration")
        parent = path.parent
        if (
            parent.is_symlink()
            or not parent.is_dir()
            or parent.stat().st_uid != os.getuid()
            or (stat.S_IMODE(parent.stat().st_mode) & 0o077)
        ):
            raise ScopeDeniedError("CONNECT gateway socket parent must be operator-private")
        self.path, self.policy, self.token = path, policy, token
        self.resolver, self.connect = resolver, connect
        self._slots = threading.BoundedSemaphore(8)
        self._server: _UnixServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            raise StateMismatchError("CONNECT gateway already running")
        gateway = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                if not gateway._slots.acquire(blocking=False):
                    self.request.sendall(b"HTTP/1.1 503 Busy\r\nConnection: close\r\n\r\n")
                    return
                try:
                    gateway._handle(self.request)
                finally:
                    gateway._slots.release()

        self._server = _UnixServer(str(self.path), Handler)
        os.chmod(self.path, 0o600)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="potbot-connect-broker"
        )
        self._thread.start()

    def _handle(self, client: socket.socket) -> None:
        client.settimeout(8)
        data = bytearray()
        while b"\r\n\r\n" not in data and len(data) < _MAX_HEADER:
            chunk = client.recv(min(1024, _MAX_HEADER - len(data)))
            if not chunk:
                return
            data.extend(chunk)
        if b"\r\n\r\n" not in data:
            client.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            return
        header, _, leftover = bytes(data).partition(b"\r\n\r\n")
        try:
            lines = header.decode("ascii").split("\r\n")
            method, authority, protocol = lines[0].split(" ")
            if method != "CONNECT" or protocol != "HTTP/1.1" or not 2 <= len(lines) <= 15:
                raise ValueError()
            fields: dict[str, str] = {}
            for line in lines[1:]:
                key, value = line.split(":", 1)
                name = key.lower()
                if not name or name in fields:
                    raise ValueError()
                fields[name] = value.strip()
            if not secrets.compare_digest(
                fields.get("proxy-authorization", ""), "Bearer " + self.token
            ):
                raise PermissionError()
            if fields.get("host", authority) != authority or (
                set(fields)
                - {"host", "proxy-authorization", "proxy-connection", "connection", "user-agent"}
            ):
                raise ValueError()
            ip = self.policy.resolve(authority, resolver=self.resolver)
        except PermissionError:
            client.sendall(
                b"HTTP/1.1 407 Proxy Authentication Required\r\nConnection: close\r\n\r\n"
            )
            return
        except (ValueError, UnicodeError, ScopeDeniedError, InvalidInputError):
            client.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            return
        try:
            upstream = self.connect((ip, 443), timeout=6)
        except OSError:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            return
        with upstream:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if leftover:
                upstream.sendall(leftover)
            sockets = [client, upstream]
            totals = {client: 0, upstream: len(leftover)}
            deadline = time.monotonic() + _MAX_SECONDS
            for sock in sockets:
                sock.setblocking(False)
            while time.monotonic() < deadline:
                readable, _, _ = select.select(sockets, [], [], 0.5)
                for source in readable:
                    chunk = source.recv(16384)
                    if not chunk:
                        return
                    totals[source] += len(chunk)
                    if totals[source] > _MAX_TUNNEL:
                        return
                    destination = upstream if source is client else client
                    # Bound send stall; never keep a tunnel alive indefinitely.
                    destination.settimeout(4)
                    try:
                        destination.sendall(chunk)
                    finally:
                        destination.setblocking(False)

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        if self.path.is_socket() and not self.path.is_symlink():
            self.path.unlink()
