"""Exact provider request routing with numeric dialing and named TLS verification."""

from __future__ import annotations

import asyncio
import socket
from functools import partial
from urllib.parse import urlsplit

import httpx

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.tools.connect_policy import ConnectPolicy, checked_host


def canonical_https_host(url: str, *, allow_query: bool = False) -> str:
    if not isinstance(url, str) or len(url) > 2048 or any(not 33 <= ord(c) <= 126 for c in url):
        raise InvalidInputError("The provider requires a canonical HTTPS URL")
    try:
        parts = urlsplit(url)
        host = checked_host(parts.hostname)
        if (
            parts.scheme != "https"
            or parts.netloc != host
            or (not allow_query and "?" in url)
            or "#" in url
            or "\\" in url
            or str(httpx.URL(url)) != url
        ):
            raise InvalidInputError(
                "The provider requires a canonical HTTPS URL on the default port"
            )
    except (ValueError, httpx.InvalidURL):
        raise InvalidInputError("The provider requires a canonical HTTPS URL") from None
    return host


class PinnedHttpsTransport(httpx.AsyncBaseTransport):
    """Enforce one method and URL before DNS, then verify TLS for the named host."""

    def __init__(
        self, method: str, url: str, *, authenticated: bool = False, allow_query: bool = False
    ) -> None:
        self.method = method
        self.url = url
        self.authenticated = authenticated
        self.host = canonical_https_host(url, allow_query=allow_query)
        self.policy = ConnectPolicy((self.host,))
        self.backend = httpx.AsyncHTTPTransport(
            retries=0, trust_env=False, limits=httpx.Limits(max_keepalive_connections=0)
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != self.method or str(request.url) != self.url:
            raise ScopeDeniedError("HTTPS request differs from the approved route")
        if self.authenticated != ("authorization" in request.headers):
            raise ScopeDeniedError("HTTPS route refused unexpected credential state")
        # The current policy dials IPv4 only. Reject the entire A answer set if
        # it includes private or special addresses. Numeric dialing avoids re-resolution.
        address = await asyncio.to_thread(
            self.policy.resolve,
            self.host + ":443",
            resolver=partial(socket.getaddrinfo, family=socket.AF_INET),
        )
        headers = httpx.Headers(request.headers)
        headers.pop("cookie", None)
        headers["Host"] = self.host
        headers["Accept-Encoding"] = "identity"
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=address),
            headers=headers,
            stream=request.stream,
            extensions={"timeout": request.extensions.get("timeout"), "sni_hostname": self.host},
        )
        response = await self.backend.handle_async_request(pinned)
        response.headers.pop("set-cookie", None)
        if response.headers.get("content-encoding", "identity").lower().strip() != "identity":
            await response.aclose()
            raise ScopeDeniedError("HTTPS route refused encoded content")
        return response

    async def aclose(self) -> None:
        await self.backend.aclose()
