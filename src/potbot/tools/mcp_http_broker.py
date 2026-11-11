"""Controller-owned HTTP boundary for explicitly approved MCP resources."""

from __future__ import annotations

import asyncio
import re
import socket
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.tools.connect_policy import ConnectPolicy, checked_host
from potbot.tools.mcp_http import McpHttpTransport
from potbot.tools.mcp_protocol import McpProtocolError, parse_json_document

_METADATA_LIMIT = 64 * 1024
_BEARER = re.compile(r"[A-Za-z0-9._~+/-]+=*")


def _https_host(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048 or any(not 33 <= ord(c) <= 126 for c in url):
        raise InvalidInputError("MCP requires a canonical HTTPS URL")
    try:
        parts = urlsplit(url)
        host = checked_host(parts.hostname)
        if (
            parts.scheme != "https"
            or parts.netloc != host
            or "?" in url
            or "#" in url
            or "\\" in url
            or str(httpx.URL(url)) != url
        ):
            raise InvalidInputError("MCP requires a canonical HTTPS URL on the default port")
    except (ValueError, httpx.InvalidURL):
        raise InvalidInputError("MCP requires a canonical HTTPS URL") from None
    return host


@dataclass(frozen=True, slots=True)
class McpHttpGrant:
    workspace_id: UUID
    principal_id: UUID
    resource: str
    issuer: str
    scopes: tuple[str, ...]

    def __post_init__(self) -> None:
        _https_host(self.resource)
        _https_host(self.issuer)
        if not isinstance(self.workspace_id, UUID) or not isinstance(self.principal_id, UUID):
            raise InvalidInputError("MCP consent requires a workspace and principal")
        if urlsplit(self.resource).path in ("", "/"):
            raise InvalidInputError("MCP resource requires an explicit path")
        if (
            type(self.scopes) is not tuple
            or not 1 <= len(self.scopes) <= 32
            or any(
                not isinstance(scope, str)
                or re.fullmatch(r"[\x21\x23-\x5B\x5D-\x7E]{1,128}", scope) is None
                for scope in self.scopes
            )
            or tuple(sorted(set(self.scopes))) != self.scopes
        ):
            raise InvalidInputError("MCP consent requires canonical explicit scopes")

    @property
    def discovery(self) -> str:
        return self.issuer.rstrip("/") + "/.well-known/openid-configuration"


@dataclass(frozen=True, slots=True)
class ScopedMcpBearer:
    """Claims from a trusted credential provider and a short-lived secret."""

    grant: McpHttpGrant
    audience: str
    expires_at: int
    secret: str = field(repr=False, compare=False)


class _PinnedHttps(httpx.AsyncBaseTransport):
    def __init__(self, method: str, url: str, *, authenticated: bool = False) -> None:
        self.method = method
        self.url = url
        self.authenticated = authenticated
        self.host = _https_host(url)
        self.policy = ConnectPolicy((self.host,))
        self.backend = httpx.AsyncHTTPTransport(
            retries=0, trust_env=False, limits=httpx.Limits(max_keepalive_connections=0)
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != self.method or str(request.url) != self.url:
            raise ScopeDeniedError("MCP request differs from the approved route")
        if self.authenticated != ("authorization" in request.headers):
            raise ScopeDeniedError("MCP route refused unexpected credential state")
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
            raise ScopeDeniedError("MCP route refused encoded content")
        return response

    async def aclose(self) -> None:
        await self.backend.aclose()


class McpHttpBroker:
    """Accepts only policy and credentials supplied by trusted controller code."""

    def __init__(
        self,
        authorize: Callable[[McpHttpGrant], bool],
        credentials: Callable[[McpHttpGrant], ScopedMcpBearer],
    ) -> None:
        self._authorize = authorize
        self._credentials = credentials

    def _require_consent(self, grant: McpHttpGrant) -> None:
        try:
            approved = self._authorize(grant)
        except Exception:
            raise ScopeDeniedError("MCP consent unavailable") from None
        if approved is not True:
            raise ScopeDeniedError("MCP resource requires explicit consent")

    def _bearer(self, grant: McpHttpGrant) -> str:
        try:
            bearer = self._credentials(grant)
        except Exception:
            raise ScopeDeniedError("MCP scoped credential unavailable") from None
        if (
            type(bearer) is not ScopedMcpBearer
            or bearer.grant != grant
            or bearer.audience != grant.resource
            or type(bearer.expires_at) is not int
            or bearer.expires_at <= time.time()
            or not isinstance(bearer.secret, str)
            or not 1 <= len(bearer.secret) <= 8192
            or _BEARER.fullmatch(bearer.secret) is None
        ):
            raise ScopeDeniedError("MCP credential does not match the approved resource")
        return bearer.secret

    async def discover_issuer(self, grant: McpHttpGrant) -> dict[str, str]:
        self._require_consent(grant)
        try:
            async with (
                asyncio.timeout(15),
                httpx.AsyncClient(
                    transport=_PinnedHttps("GET", grant.discovery), trust_env=False
                ) as client,
                client.stream("GET", grant.discovery, follow_redirects=False) as response,
            ):
                if response.status_code != 200 or (
                    response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    != "application/json"
                ):
                    raise ScopeDeniedError("MCP issuer metadata unavailable")
                body = bytearray()
                async for piece in response.aiter_bytes(chunk_size=4096):
                    if len(body) + len(piece) > _METADATA_LIMIT:
                        raise ScopeDeniedError("MCP issuer metadata exceeds its budget")
                    body.extend(piece)
                document = parse_json_document(bytes(body))
        except (McpProtocolError, httpx.HTTPError, OSError, TimeoutError):
            raise ScopeDeniedError("MCP issuer metadata unavailable") from None

        if not isinstance(document, dict) or document.get("issuer") != grant.issuer:
            raise ScopeDeniedError("MCP issuer metadata identity mismatch")
        endpoints = {"issuer": grant.issuer}
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            url = document.get(name)
            if not isinstance(url, str):
                raise ScopeDeniedError("MCP issuer endpoint is missing")
            _https_host(url)
            endpoints[name] = url
        return endpoints

    @asynccontextmanager
    async def open(self, grant: McpHttpGrant) -> AsyncIterator[McpHttpTransport]:
        await self.discover_issuer(grant)

        def authorize(url: str) -> None:
            if url != grant.resource:
                raise ScopeDeniedError("MCP resource changed")
            self._require_consent(grant)

        async with httpx.AsyncClient(
            transport=_PinnedHttps("POST", grant.resource, authenticated=True), trust_env=False
        ) as client:
            yield McpHttpTransport(
                grant.resource,
                client,
                network_approval=authorize,
                bearer_supplier=lambda: self._bearer(grant),
            )
