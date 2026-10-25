"""Operator-only, opt-in HTTPS CONNECT egress broker (AF_UNIX only).

This does NOT enable Chromium network access. The ordinary sandbox still has a
private network namespace and denies TCP creation; connecting Chromium to this
broker requires a separately qualified, isolated namespace-local HTTP forwarder.
No host TCP listener, broad fetch API, URL forwarding or DNS rebinding fallback.
"""
from __future__ import annotations

import ipaddress
import os
import re
import secrets
import select
import socket
import socketserver
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError

_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")
_MAX_HEADER = 4096
_MAX_TUNNEL = 16 * 1024 * 1024
_MAX_SECONDS = 60


def checked_host(host: object) -> str:
    """Allow only canonical ASCII DNS names (never IPs, Unicode/IDNA aliases)."""
    if not isinstance(host, str) or host != host.lower() or len(host) > 253 or (
        not _HOST.fullmatch(host) or "." not in host or host.endswith(".")
        or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
               or label.startswith("xn--") for label in host.split("."))
    ):
        raise InvalidInputError("CONNECT host must be a canonical ASCII DNS name")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ScopeDeniedError("CONNECT to IP addresses is forbidden")


@dataclass(frozen=True, slots=True)
class ConnectPolicy:
    hosts: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.hosts, tuple) or not 1 <= len(self.hosts) <= 32:
            raise InvalidInputError("CONNECT requires a small explicit exact-host allowlist")
        for h in self.hosts:
            checked_host(h)
        if len(set(self.hosts)) != len(self.hosts):
            raise InvalidInputError("duplicate CONNECT policy host")

    def resolve(self, authority: str, *, resolver: Callable[..., object] = socket.getaddrinfo) -> str:
        if not isinstance(authority, str) or authority.count(":") != 1:
            raise ScopeDeniedError("CONNECT must specify one approved hostname and TLS port")
        host, port = authority.split(":", 1)
        host = checked_host(host)
        if host not in self.hosts or port != "443":
            raise ScopeDeniedError("CONNECT destination is not on the operator policy")
        try:
            answers = resolver(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        except OSError as error:
            raise ScopeDeniedError("approved destination is not currently resolvable") from error
        if not answers or len(answers) > 64:
            raise ScopeDeniedError("CONNECT DNS answer is empty or excessive")
        addresses = []
        for answer in answers:
            address = answer[4][0]
            try:
                ip = ipaddress.ip_address(address)
            except ValueError as error:
                raise ScopeDeniedError("unexpected CONNECT DNS address") from error
            if ip.version == 6 and ip.ipv4_mapped is not None:
                ip = ip.ipv4_mapped
            # Entire answer must be public: mixed public/private is suspect.
            if not ip.is_global or ip.is_multicast or ip.is_unspecified:
                raise ScopeDeniedError("CONNECT DNS includes a private or special address")
            addresses.append(str(ip))
        # connect *directly* to the checked literal IP; never resolve hostname
        # a second time inside the transport, avoiding classic DNS rebinding.
        return sorted(set(addresses))[0]


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


class BrowserConnectGateway:
    """Small operator-bound CONNECT relay, never a general forward proxy."""
    def __init__(self, socket_path: Path, *, policy: ConnectPolicy, token: str,
                 resolver: Callable[..., object] = socket.getaddrinfo,
                 connect: Callable[..., socket.socket] = socket.create_connection) -> None:
        path = Path(socket_path).absolute()
        if (not isinstance(policy, ConnectPolicy) or not isinstance(token, str)
                or len(token) < 32 or len(token) > 128 or "\r" in token or "\n" in token
                or path.is_symlink() or path.exists()):
            raise ScopeDeniedError("invalid private CONNECT gateway configuration")
        parent = path.parent
        if parent.is_symlink() or not parent.is_dir() or parent.stat().st_uid != os.getuid() or (
            stat.S_IMODE(parent.stat().st_mode) & 0o077
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
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True,
                                        name="potbot-connect-broker")
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
            if not secrets.compare_digest(fields.get("proxy-authorization", ""),
                                           "Bearer " + self.token):
                raise PermissionError()
            if fields.get("host", authority) != authority or (
                set(fields) - {"host", "proxy-authorization", "proxy-connection", "connection", "user-agent"}
            ):
                raise ValueError()
            ip = self.policy.resolve(authority, resolver=self.resolver)
        except PermissionError:
            client.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nConnection: close\r\n\r\n")
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
                readable, _, _ = select.select(sockets, [], [], .5)
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
