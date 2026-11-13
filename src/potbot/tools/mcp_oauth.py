"""Operator-registered authorization-code acquisition for an MCP HTTP grant."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from potbot.credentials import CredentialStore
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools.mcp_http_broker import (
    McpHttpBroker,
    McpHttpGrant,
    ScopedMcpBearer,
    _pinned_json,
)
from potbot.tools.mcp_protocol import McpProtocolError, parse_json_document
from potbot.tools.pinned_https import canonical_https_host

_B64 = re.compile(r"[A-Za-z0-9_-]+")
_TOKEN_LIMIT = 4096
_ATTEMPT_SECONDS = 300


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(part: str) -> bytes:
    if _B64.fullmatch(part) is None:
        raise ValueError("invalid JWT encoding")
    raw = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    if _base64url(raw) != part:
        raise ValueError("noncanonical JWT encoding")
    return raw


def _visible_text(value: object, limit: int) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= limit
        and all(33 <= ord(char) <= 126 for char in value)
    )


@dataclass(frozen=True, slots=True)
class McpOAuthRegistration:
    client_id: str
    subject: str
    callback: str
    authorization_endpoint: str
    token_endpoint: str
    key_id: str
    signing_key_pem: bytes = field(repr=False)

    def __post_init__(self) -> None:
        for endpoint in (self.callback, self.authorization_endpoint, self.token_endpoint):
            canonical_https_host(endpoint)
            if urlsplit(endpoint).path in ("", "/"):
                raise InvalidInputError("MCP OAuth endpoints require explicit paths")
        if any(
            not _visible_text(value, 255) for value in (self.client_id, self.subject, self.key_id)
        ):
            raise InvalidInputError(
                "MCP OAuth registration has an invalid client, subject or key ID"
            )


@dataclass(frozen=True, slots=True, repr=False)
class _PendingLogin:
    state: str
    verifier: str
    expires_at: float


class McpOAuthProvider:
    """One controller-authorized workspace, principal and resource per provider."""

    def __init__(
        self,
        grant: McpHttpGrant,
        registration: McpOAuthRegistration,
        authorize: Callable[[McpHttpGrant], bool],
        store: CredentialStore,
    ) -> None:
        self.grant = grant
        self.registration = registration
        self.store = store
        self.broker = McpHttpBroker(authorize, self)
        self._pending: _PendingLogin | None = None
        self._login = asyncio.Lock()
        self._signing_key = self._load_signing_key(registration.signing_key_pem)
        binding = canonical_json(
            {
                "workspace": str(grant.workspace_id),
                "principal": str(grant.principal_id),
                "resource": grant.resource,
                "issuer": grant.issuer,
                "scopes": list(grant.scopes),
                "client": registration.client_id,
                "subject": registration.subject,
            }
        )
        self._account = "mcp.oauth." + hashlib.sha256(binding.encode()).hexdigest()
        self.registration_fingerprint = content_hash(
            {
                "binding": binding,
                "callback": registration.callback,
                "authorization_endpoint": registration.authorization_endpoint,
                "token_endpoint": registration.token_endpoint,
                "key_id": registration.key_id,
                "signing_key_sha256": hashlib.sha256(registration.signing_key_pem).hexdigest(),
            }
        )

    @staticmethod
    def _load_signing_key(pem: bytes) -> rsa.RSAPublicKey:
        try:
            if not isinstance(pem, bytes) or len(pem) > 4096:
                raise ValueError("invalid signing key size")
            key = serialization.load_pem_public_key(pem)
        except (TypeError, ValueError):
            raise InvalidInputError(
                "MCP OAuth requires an operator-pinned RSA public key"
            ) from None
        if not isinstance(key, rsa.RSAPublicKey) or not 2048 <= key.key_size <= 4096:
            raise InvalidInputError("MCP OAuth requires an operator-pinned RSA public key")
        return key

    def _verify_token(self, token: str) -> ScopedMcpBearer:
        try:
            if not _visible_text(token, _TOKEN_LIMIT):
                raise ValueError("invalid token")
            header_part, claims_part, signature = token.split(".")
            header = parse_json_document(_decode(header_part))
            if not isinstance(header, dict) or (
                set(header) != {"alg", "typ", "kid"}
                or header["alg"] != "RS256"
                or header["typ"] not in ("at+jwt", "application/at+jwt")
                or header["kid"] != self.registration.key_id
            ):
                raise ValueError("invalid token header")
            self._signing_key.verify(
                _decode(signature),
                f"{header_part}.{claims_part}".encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            claims = parse_json_document(_decode(claims_part))
            if not isinstance(claims, dict):
                raise ValueError("invalid token claims")
            now = int(time.time())
            issued, expires = claims.get("iat"), claims.get("exp")
            not_before = claims.get("nbf", issued)
            scope = claims.get("scope")
            audience = claims.get("aud")
            if (
                claims.get("iss") != self.grant.issuer
                or claims.get("sub") != self.registration.subject
                or claims.get("client_id") != self.registration.client_id
                or audience not in (self.grant.resource, [self.grant.resource])
                or not _visible_text(claims.get("jti"), 255)
                or "cnf" in claims
                or "act" in claims
                or not isinstance(scope, str)
                or tuple(sorted(scope.split(" "))) != self.grant.scopes
                or type(issued) is not int
                or type(not_before) is not int
                or type(expires) is not int
                or not issued <= now < expires
                or not_before > now
            ):
                raise ValueError("unapproved token claims")
        except (ValueError, InvalidSignature, McpProtocolError):
            raise ScopeDeniedError("MCP OAuth access token failed verification") from None
        return ScopedMcpBearer(self.grant, self.grant.resource, expires, token)

    def __call__(self, grant: McpHttpGrant) -> ScopedMcpBearer:
        if grant != self.grant:
            raise ScopeDeniedError("MCP OAuth provider cannot serve a different grant")
        self.broker.require_consent(grant)
        token = self.store.get(self._account)
        if token is None:
            raise ScopeDeniedError("MCP OAuth login required")
        return self._verify_token(token)

    async def begin(self) -> str:
        """Return the URL for the controller's user-authorized browser handoff."""
        async with self._login:
            self._pending = None
            metadata = await self.broker.discover_issuer(self.grant)
            registered = self.registration
            if (
                metadata["authorization_endpoint"] != registered.authorization_endpoint
                or metadata["token_endpoint"] != registered.token_endpoint
            ):
                raise ScopeDeniedError("MCP OAuth issuer endpoints differ from the registration")
            if (
                metadata.get("code_challenge_method") != "S256"
                or metadata.get("authorization_response_issuer") != self.grant.issuer
            ):
                raise ScopeDeniedError(
                    "MCP OAuth issuer must advertise S256 and callback issuer support"
                )
            attempt = _PendingLogin(
                secrets.token_urlsafe(32),
                secrets.token_urlsafe(32),
                time.monotonic() + _ATTEMPT_SECONDS,
            )
            self._pending = attempt
            query = urlencode(
                {
                    "response_type": "code",
                    "client_id": registered.client_id,
                    "redirect_uri": registered.callback,
                    "resource": self.grant.resource,
                    "scope": " ".join(self.grant.scopes),
                    "state": attempt.state,
                    "code_challenge": _base64url(
                        hashlib.sha256(attempt.verifier.encode()).digest()
                    ),
                    "code_challenge_method": "S256",
                }
            )
            return registered.authorization_endpoint + "?" + query

    async def complete(self, callback_url: str) -> None:
        """Consume one matching HTTPS callback before attempting code exchange."""
        async with self._login:
            self.broker.require_consent(self.grant)
            attempt = self._pending
            if attempt is None or attempt.expires_at <= time.monotonic():
                self._pending = None
                raise ScopeDeniedError("MCP OAuth login has no live attempt")
            if not _visible_text(callback_url, 8192):
                raise ScopeDeniedError("MCP OAuth callback is invalid")
            endpoint, _, query = callback_url.partition("?")
            if endpoint != self.registration.callback or "#" in query:
                raise ScopeDeniedError("MCP OAuth callback is invalid")
            try:
                fields = parse_qs(
                    query,
                    strict_parsing=True,
                    keep_blank_values=True,
                    max_num_fields=3,
                    errors="strict",
                )
            except (ValueError, UnicodeError):
                raise ScopeDeniedError("MCP OAuth callback is invalid") from None
            if set(fields) != {"code", "state", "iss"} or any(
                len(values) != 1 for values in fields.values()
            ):
                raise ScopeDeniedError("MCP OAuth callback is invalid")
            code, state, issuer = (fields[name][0] for name in ("code", "state", "iss"))
            if (
                issuer != self.grant.issuer
                or not _visible_text(code, 2048)
                or not _visible_text(state, 128)
                or not hmac.compare_digest(state, attempt.state)
            ):
                raise ScopeDeniedError("MCP OAuth callback is invalid")
            self._pending = None
            document = await _pinned_json(
                "POST",
                self.registration.token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "client_id": self.registration.client_id,
                    "code": code,
                    "redirect_uri": self.registration.callback,
                    "code_verifier": attempt.verifier,
                    "resource": self.grant.resource,
                },
            )
            if (
                not isinstance(document, dict)
                or not isinstance(document.get("token_type"), str)
                or document["token_type"].lower() != "bearer"
                or not isinstance(document.get("access_token"), str)
            ):
                raise ScopeDeniedError("MCP OAuth issuer did not supply a bearer access token")
            bearer = self._verify_token(document["access_token"])
            self.broker.require_consent(self.grant)
            self.store.set(self._account, bearer.secret)

    async def forget(self) -> bool:
        """Clear the pending login and remove this grant's locally held bearer."""
        async with self._login:
            self._pending = None
            return self.store.delete(self._account)
