"""Hosted browser handoff for one operator-registered MCP OAuth grant."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from potbot.api.ui_api_setup import ApiSetupCredentials
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import DomainError, InvalidInputError, ScopeDeniedError
from potbot.service_credentials import ServiceCredentials
from potbot.tools.mcp_http_broker import McpHttpGrant
from potbot.tools.mcp_oauth import McpOAuthProvider, McpOAuthRegistration
from potbot.tools.mcp_protocol import McpClient, parse_json_document

CALLBACK_PATH = "/v1/mcp/oauth/callback"
_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; base-uri 'none'; frame-ancestors 'none'",
}
_FIELDS = {
    "resource",
    "issuer",
    "scopes",
    "client_id",
    "subject",
    "callback",
    "authorization_endpoint",
    "token_endpoint",
    "key_id",
    "signing_key_pem",
}


@dataclass(frozen=True, slots=True)
class HostedMcpOAuth:
    provider: McpOAuthProvider
    authorize_setup: Callable[[str | None], None]

    def __post_init__(self) -> None:
        if urlsplit(self.provider.registration.callback).path != CALLBACK_PATH:
            raise InvalidInputError("MCP registration must use the hosted callback path")

    def install(
        self,
        app: FastAPI,
        authenticate: Callable[[Request], WorkspaceContext],
    ) -> None:
        provider = self.provider
        expected_host = urlsplit(provider.registration.callback).netloc

        async def operator(request: Request) -> None:
            actor = authenticate(request)
            if (
                actor.workspace_id != provider.grant.workspace_id
                or actor.principal_id != provider.grant.principal_id
                or request.scope.get("query_string", b"")
            ):
                raise ScopeDeniedError("MCP OAuth operator scope denied")
            await run_in_threadpool(
                self.authorize_setup, request.headers.get("x-potbot-setup-token")
            )

        @app.post("/v1/mcp/oauth/start")
        async def start(request: Request) -> JSONResponse:
            await operator(request)
            return JSONResponse({"authorization_url": await provider.begin()}, headers=_HEADERS)

        @app.get(CALLBACK_PATH, include_in_schema=False)
        async def callback(request: Request) -> HTMLResponse:
            # Uvicorn does not trust forwarded headers. Check the original authority
            # preserved by the TLS proxy and reconstruct only the registered URL.
            query = request.scope.get("query_string", b"")
            try:
                if (
                    request.headers.getlist("host") != [expected_host]
                    or request.scope.get("raw_path") != CALLBACK_PATH.encode("ascii")
                    or not 1 <= len(query) <= 8192
                ):
                    raise ScopeDeniedError("MCP callback does not match the registration")
                await provider.complete(
                    provider.registration.callback + "?" + query.decode("ascii", "strict")
                )
            except (DomainError, UnicodeError):
                return HTMLResponse(
                    "<p>MCP authorization failed. Return to Potbot to start again.</p>",
                    status_code=400,
                    headers=_HEADERS,
                )
            return HTMLResponse(
                "<p>MCP authorization complete. Return to Potbot.</p>",
                headers=_HEADERS,
            )

        @app.post("/v1/mcp/oauth/check")
        async def check(request: Request) -> JSONResponse:
            await operator(request)
            async with provider.broker.open(provider.grant) as transport:
                client = McpClient(transport.exchange)
                catalog = await client.refresh_tools()
            return JSONResponse(
                {
                    "protocol": client.protocol_version,
                    "catalog_sha256": catalog.fingerprint,
                    "tool_count": len(catalog.tools),
                },
                headers=_HEADERS,
            )


def read_mcp_oauth_registration(
    path: Path,
    actor: WorkspaceContext,
    credentials: ServiceCredentials,
) -> HostedMcpOAuth:
    try:
        with path.open("rb") as file:
            raw = file.read(16_385)
        if len(raw) > 16_384:
            raise ValueError("registration exceeds bound")
        document = parse_json_document(raw)
        if not isinstance(document, dict) or set(document) != _FIELDS:
            raise ValueError("unexpected registration fields")
        scopes = document["scopes"]
        if (
            not isinstance(scopes, list)
            or any(not isinstance(scope, str) for scope in scopes)
            or any(not isinstance(document[name], str) for name in _FIELDS - {"scopes"})
        ):
            raise ValueError("invalid registration fields")
        grant = McpHttpGrant(
            actor.workspace_id,
            actor.principal_id,
            document["resource"],
            document["issuer"],
            tuple(scopes),
        )
        registration = McpOAuthRegistration(
            client_id=document["client_id"],
            subject=document["subject"],
            callback=document["callback"],
            authorization_endpoint=document["authorization_endpoint"],
            token_endpoint=document["token_endpoint"],
            key_id=document["key_id"],
            signing_key_pem=document["signing_key_pem"].encode("ascii"),
        )
        provider = McpOAuthProvider(
            grant, registration, lambda candidate: candidate == grant, credentials.store
        )
        return HostedMcpOAuth(provider, ApiSetupCredentials(credentials).authorize_setup)
    except (DomainError, OSError, ValueError, UnicodeError):
        raise InvalidInputError("Invalid operator MCP OAuth registration") from None
