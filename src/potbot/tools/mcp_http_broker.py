"""Controller-owned HTTP boundary for explicitly approved MCP resources."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.tools.mcp_http import McpHttpTransport
from potbot.tools.mcp_protocol import McpProtocolError, McpToolInfo, parse_json_document
from potbot.tools.pinned_https import PinnedHttpsTransport, canonical_https_host

_METADATA_LIMIT = 64 * 1024
_BEARER = re.compile(r"[A-Za-z0-9._~+/-]+=*")


@dataclass(frozen=True, slots=True)
class McpHttpGrant:
    workspace_id: UUID
    principal_id: UUID
    resource: str
    issuer: str
    scopes: tuple[str, ...]

    def __post_init__(self) -> None:
        canonical_https_host(self.resource)
        canonical_https_host(self.issuer)
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


async def _pinned_json(method: str, url: str, *, data: dict[str, str] | None = None) -> object:
    try:
        async with (
            asyncio.timeout(15),
            httpx.AsyncClient(
                transport=PinnedHttpsTransport(method, url), trust_env=False
            ) as client,
            client.stream(method, url, data=data, follow_redirects=False) as response,
        ):
            kind = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if response.status_code != 200 or kind != "application/json":
                raise ScopeDeniedError("MCP issuer returned an unexpected response")
            body = bytearray()
            async for piece in response.aiter_bytes(chunk_size=4096):
                if len(body) + len(piece) > _METADATA_LIMIT:
                    raise ScopeDeniedError("MCP issuer response exceeds its budget")
                body.extend(piece)
            return parse_json_document(bytes(body))
    except (McpProtocolError, httpx.HTTPError, OSError, TimeoutError):
        raise ScopeDeniedError("MCP issuer response unavailable") from None


class McpHttpBroker:
    """Accepts only policy and credentials supplied by trusted controller code."""

    def __init__(
        self,
        authorize: Callable[[McpHttpGrant], bool],
        credentials: Callable[[McpHttpGrant], ScopedMcpBearer],
    ) -> None:
        self._authorize = authorize
        self._credentials = credentials

    def require_consent(self, grant: McpHttpGrant) -> None:
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
        self.require_consent(grant)
        document = await _pinned_json("GET", grant.discovery)

        if not isinstance(document, dict) or document.get("issuer") != grant.issuer:
            raise ScopeDeniedError("MCP issuer metadata identity mismatch")
        endpoints = {"issuer": grant.issuer}
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            url = document.get(name)
            if not isinstance(url, str):
                raise ScopeDeniedError("MCP issuer endpoint is missing")
            canonical_https_host(url)
            endpoints[name] = url
        methods = document.get("code_challenge_methods_supported")
        if isinstance(methods, list) and "S256" in methods:
            endpoints["code_challenge_method"] = "S256"
        if document.get("authorization_response_iss_parameter_supported") is True:
            endpoints["authorization_response_issuer"] = grant.issuer
        return endpoints

    @asynccontextmanager
    async def open(
        self, grant: McpHttpGrant, *, header_tool: McpToolInfo | None = None
    ) -> AsyncIterator[McpHttpTransport]:
        await self.discover_issuer(grant)

        def authorize(url: str) -> None:
            if url != grant.resource:
                raise ScopeDeniedError("MCP resource changed")
            self.require_consent(grant)

        async with httpx.AsyncClient(
            transport=PinnedHttpsTransport("POST", grant.resource, authenticated=True),
            trust_env=False,
        ) as client:
            yield McpHttpTransport(
                grant.resource,
                client,
                network_approval=authorize,
                bearer_supplier=lambda: self._bearer(grant),
                header_tool=header_tool,
            )
