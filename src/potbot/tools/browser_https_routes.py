"""Sandbox-side, GET/HEAD-only HTTPS fetch through the operator's AF_UNIX CONNECT broker.

Chromium and Playwright still have no AF_INET/AF_INET6 permission. Its ordinary
network requests are aborted or fulfilled from this independently TLS-verified
request; no localhost proxy, firewall change, implicit redirect, or direct
network fallback exists. Browser content is always untrusted.
"""
from __future__ import annotations

import asyncio
import http.client
import ipaddress
import os
import re
import socket
import stat
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
from typing import Any, Callable

MAX_BODY = 4 * 1024 * 1024
MAX_HEADER = 4096
CONNECT_SECONDS = 6.0
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")
_HEADER_KEEP = frozenset({
    "content-type", "cache-control", "content-security-policy", "x-content-type-options",
    "access-control-allow-origin", "access-control-allow-credentials",
    "access-control-expose-headers", "cross-origin-resource-policy",
    "cross-origin-opener-policy", "cross-origin-embedder-policy", "location",
})


class EgressDenied(RuntimeError):
    """An unavailable or unauthorized remote request, without secret-bearing details."""


@dataclass(frozen=True, slots=True)
class RoutedResponse:
    status: int
    headers: dict[str, str]
    body: bytes


def checked_target(url: str, method: str) -> tuple[str, str]:
    """Validate URL syntax before giving *any* host bytes to the broker."""
    if (not isinstance(url, str) or len(url) > 2048 or not url.isascii() or
            any(ord(c) < 33 or ord(c) == 127 for c in url) or method not in {"GET", "HEAD"}):
        raise EgressDenied("browser egress method or URL denied")
    try:
        target = urlsplit(url)
        host = target.hostname or ""
        if (target.scheme != "https" or target.username is not None or target.password is not None
                or target.port not in (None, 443) or not _HOST.fullmatch(host) or
                host != host.lower() or host.endswith(".") or "." not in host or
                any(not part or part.startswith("-") or part.endswith("-") or part.startswith("xn--")
                    or len(part) > 63 for part in host.split(".")) or
                target.netloc != host and target.netloc != host + ":443"):
            raise EgressDenied("browser egress destination denied")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise EgressDenied("literal IP browser destinations are forbidden")
        request_target = urlunsplit(("", "", target.path or "/", target.query, ""))
        if len(request_target.encode("ascii")) > 2048:
            raise EgressDenied("browser egress path too long")
        return host, request_target
    except (UnicodeError, ValueError):
        raise EgressDenied("browser egress URL invalid") from None


def _read_connect_header(sock: socket.socket) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data and len(data) < MAX_HEADER:
        chunk = sock.recv(min(512, MAX_HEADER - len(data)))
        if not chunk:
            raise EgressDenied("CONNECT gateway closed the socket")
        data.extend(chunk)
    header, separator, extra = bytes(data).partition(b"\r\n\r\n")
    if not separator or extra or not header.startswith(b"HTTP/1.1 200 "):
        raise EgressDenied("CONNECT gateway denied destination")
    return header


def fetch_https(url: str, method: str, *, socket_path: str, token: str,
                create_socket: Callable[[], socket.socket] = lambda: socket.socket(socket.AF_UNIX),
                ssl_context: ssl.SSLContext | None = None) -> RoutedResponse:
    """No trust_env, no redirects, no insecure TLS, no web-controlled proxy headers."""
    host, target = checked_target(url, method)
    if (not isinstance(socket_path, str) or not socket_path.startswith("/home/.tmp/.potbot-connect/")
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token)):
        raise EgressDenied("operator broker capability unavailable")
    context = ssl_context or ssl.create_default_context()
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise EgressDenied("verified HTTPS connection required")
    with create_socket() as channel:
        channel.settimeout(CONNECT_SECONDS)
        channel.connect(socket_path)
        header = (f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n"
                  f"Proxy-Authorization: Bearer {token}\r\nConnection: close\r\n\r\n")
        channel.sendall(header.encode("ascii"))
        _read_connect_header(channel)
        # TLS verification happens inside the sandbox using the image's pinned
        # certificate store. The operator relays ciphertext, not HTTP content.
        with context.wrap_socket(channel, server_hostname=host) as secure:
            secure.settimeout(CONNECT_SECONDS)
            request = (f"{method} {target} HTTP/1.1\r\nHost: {host}\r\n"
                       "Accept: */*\r\nAccept-Encoding: identity\r\n"
                       "User-Agent: PotbotBrowser/1\r\nConnection: close\r\n\r\n")
            secure.sendall(request.encode("ascii"))
            reply = http.client.HTTPResponse(secure)
            reply.begin()
            try:
                if (not 100 <= reply.status <= 599 or
                        reply.getheader("Content-Encoding", "identity").lower() not in ("identity", "")):
                    raise EgressDenied("remote response encoding denied")
                body = reply.read(MAX_BODY + 1) if method != "HEAD" else b""
                if len(body) > MAX_BODY:
                    raise EgressDenied("remote response size exceeded")
                headers: dict[str, str] = {}
                for key, value in reply.getheaders():
                    lower = key.lower()
                    if lower in _HEADER_KEEP and lower not in headers and len(value) <= 2048 and not any(
                            c in value for c in ("\r", "\n", "\x00")):
                        headers[lower] = value
                return RoutedResponse(reply.status, headers, body)
            finally:
                reply.close()


def _read_token_file(path: str) -> str:
    if path != "/home/.tmp/.potbot-connect/token":
        raise EgressDenied("unexpected CONNECT token mount")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise EgressDenied("insecure CONNECT token mount")
            raw = os.read(fd, 129)
        finally:
            os.close(fd)
    except OSError as error:
        raise EgressDenied("CONNECT token mount unavailable") from error
    try:
        token = raw.decode("ascii")
    except UnicodeError as error:
        raise EgressDenied("invalid CONNECT token") from error
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise EgressDenied("invalid CONNECT token")
    return token


def require_egress_config(environment: dict[str, str] | None = None, *,
                          read_token: Callable[[str], str] = _read_token_file) -> tuple[str, str] | None:
    """Only the operator's readonly FD-backed capability can enable guest fetches."""
    env = os.environ if environment is None else environment
    sock = env.get("POTBOT_BROWSER_CONNECT_SOCKET")
    token_file = env.get("POTBOT_BROWSER_CONNECT_TOKEN_FILE")
    if sock is None and token_file is None:
        return None
    if (not isinstance(sock, str) or sock != "/home/.tmp/.potbot-connect/egress.sock"
            or token_file != "/home/.tmp/.potbot-connect/token"):
        raise EgressDenied("partial or invalid browser egress grant")
    token = read_token(token_file)
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise EgressDenied("invalid browser egress token")
    return sock, token


async def install_https_routes(context: Any, socket_path: str, token: str,
                               *, fetch: Callable[..., RoutedResponse] = fetch_https) -> None:
    """Install before first page; any failure must abort, never route.continue_."""
    semaphore = asyncio.Semaphore(4)

    async def intercept(route: Any) -> None:
        request = route.request
        try:
            checked_target(request.url, request.method)
            async with semaphore:
                data = await asyncio.wait_for(asyncio.to_thread(
                    fetch, request.url, request.method, socket_path=socket_path, token=token,
                ), timeout=10)
            if not isinstance(data, RoutedResponse) or not 100 <= data.status <= 599 or len(data.body) > MAX_BODY:
                raise EgressDenied("invalid broker response")
            await route.fulfill(status=data.status, headers=data.headers, body=data.body)
        except (Exception, asyncio.CancelledError):
            await route.abort("blockedbyclient")
    await context.route("**/*", intercept)
