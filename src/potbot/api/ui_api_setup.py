"""Operator-only, value-blind management of hosted API credentials.

Keys remain in the *existing native OS vault* under the service's credential
profile. This service is deliberately not an agent-facing Cog, configuration
file, workspace RLS secret table, or a general key/value storage endpoint.

Both the ordinary workspace bearer and a separate operator setup capability
must be supplied to every request. The latter is stored as ``api.setup_token``
in the native vault and is sent only by the server-side Next.js bridge.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.models.providers import HostedProviderClient, ProviderAPIError
from potbot.service_credentials import PROVIDER_ENVIRONMENT, ServiceCredentials

if TYPE_CHECKING:
    from potbot.domain.auth import WorkspaceContext
    from potbot.persistence.database import Database

# Exact allowlist: user-supplied values never become vault account names,
# URLs, environment variable names, or arbitrary external endpoints.
_PROVIDER_LABELS = {
    "openai": "OpenAI",
    "anthropic": "Anthropic (Claude)",
    "gemini": "Google Gemini",
    "xai": "xAI (Grok)",
    "evolink": "EvoLink",
}
_PROVIDER_NAMES = tuple(name for name in _PROVIDER_LABELS if name in PROVIDER_ENVIRONMENT)
_SETUP_ACCOUNT = "api.setup_token"
_MAX_KEY_BYTES = 4096
_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'",
}


def _provider(value: str) -> str:
    if not isinstance(value, str) or value not in _PROVIDER_NAMES:
        raise InvalidInputError("Unsupported hosted API provider")
    return value


def _valid_key(value: object) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= _MAX_KEY_BYTES
            or any(not 33 <= ord(char) <= 126 for char in value)):
        raise InvalidInputError("API key must be a bounded visible ASCII token")
    return value


def _valid_setup_token(value: object) -> bool:
    return isinstance(value, str) and 32 <= len(value) <= 4096 and all(
        33 <= ord(char) <= 126 for char in value
    )


def _test_models(credentials: ServiceCredentials, provider: str) -> int:
    """Metadata-only HTTPS GET; never call a paid/generative POST endpoint."""
    client = HostedProviderClient(credentials.resolve)
    try:
        return len(client.list_models(provider))
    finally:
        client.close()


class ApiSetupCredentials:
    """Value-blind native-vault CRUD and explicit metadata connectivity tests.

    The profile is operator-global, NOT per-workspace. Database workspace
    authentication is performed by the route before any action against the
    vault. A distinct setup token is mandatory for all operations.
    """

    def __init__(
        self,
        credentials: ServiceCredentials,
        *,
        changed: Callable[[str], None] | None = None,
        probe: Callable[[ServiceCredentials, str], int] = _test_models,
    ) -> None:
        self.credentials = credentials
        self.changed = changed
        self.probe = probe
        self._mutation_lock = threading.RLock()
        self._test_lock = threading.Lock()

    def authorize_setup(self, presented: str | None) -> None:
        # Do not use the workspace's general API bearer as a vault mutation
        # capability. No secret is returned to a client, even on failure.
        if not _valid_setup_token(presented):
            raise ScopeDeniedError("Operator setup permission required")
        expected = self.credentials.store.get(_SETUP_ACCOUNT)
        if not _valid_setup_token(expected):
            raise CapabilityUnavailableError("Operator setup is not provisioned")
        assert isinstance(presented, str) and isinstance(expected, str)
        if not hmac.compare_digest(presented, expected):
            raise ScopeDeniedError("Operator setup permission required")

    def _state(self, provider: str) -> dict[str, Any]:
        name = _provider(provider)
        variable = PROVIDER_ENVIRONMENT[name]
        override = self.credentials.environment.get(variable)
        if override is not None and override != "":
            try:
                _valid_key(override)
            except InvalidInputError:
                status = "invalid_environment"
            else:
                status = "configured"
            return {
                "id": name,
                "label": _PROVIDER_LABELS[name],
                "configured": status == "configured",
                "status": status,
                "source": "environment",
                "editable": False,
                "environmentVariable": variable,
            }
        try:
            stored = self.credentials.store.get("providers." + name)
            status = "configured" if stored is not None and _valid_key_safe(stored) else (
                "invalid_vault" if stored is not None else "missing"
            )
        except CapabilityUnavailableError:
            status = "vault_unavailable"
        return {
            "id": name,
            "label": _PROVIDER_LABELS[name],
            "configured": status == "configured",
            "status": status,
            "source": "native_vault" if status in {"configured", "invalid_vault"} else None,
            "editable": status != "vault_unavailable",
            "environmentVariable": variable,
        }

    def catalog(self) -> dict[str, Any]:
        return {
            "schema": "potbot-api-setup/v1",
            "scope": "operator_credential_profile",
            "profile": self.credentials.store.profile,
            "storage": "native_os_credential_vault",
            "secretsExposed": False,
            "providers": [self._state(provider) for provider in _PROVIDER_NAMES],
        }

    def put(self, provider: str, key: object, *, confirm: bool, replace: bool) -> dict[str, Any]:
        name = _provider(provider)
        if confirm is not True or type(replace) is not bool:
            raise InvalidInputError("Explicit API credential confirmation required")
        value = _valid_key(key)
        with self._mutation_lock:
            state = self._state(name)
            if not state["editable"]:
                raise StateMismatchError("Environment override or vault prevents editing")
            if state["configured"] and not replace:
                raise StateMismatchError("Credential exists; explicitly approve rotation")
            if state["status"] == "invalid_vault" and not replace:
                raise StateMismatchError("Replacing an invalid vault credential requires confirmation")
            self.credentials.store.set("providers." + name, value)
            if self.changed is not None:
                self.changed(name)
            return self._state(name)

    def delete(self, provider: str, *, confirm: bool) -> dict[str, Any]:
        name = _provider(provider)
        if confirm is not True:
            raise InvalidInputError("Explicit API credential deletion confirmation required")
        with self._mutation_lock:
            state = self._state(name)
            if not state["editable"]:
                raise StateMismatchError("Environment override or vault prevents editing")
            deleted = self.credentials.store.delete("providers." + name)
            if deleted and self.changed is not None:
                self.changed(name)
            return {**self._state(name), "deleted": deleted}

    def test(self, provider: str) -> dict[str, Any]:
        name = _provider(provider)
        state = self._state(name)
        if not state["configured"]:
            return {"id": name, "verified": False, "status": "not_configured"}
        # Only one outbound probe per service process at a time. Deployment
        # rate limiting must also be enforced at the reverse proxy/edge.
        if not self._test_lock.acquire(blocking=False):
            raise StateMismatchError("A provider connectivity test is already running")
        try:
            try:
                models = self.probe(self.credentials, name)
                if type(models) is not int or not 0 <= models <= 10000:
                    raise ValueError("Invalid metadata probe count")
            except ProviderAPIError as error:
                # Emit a small category; never reflect a remote error body,
                # HTTP headers, raw URL, key or exception text.
                status = (
                    "authentication_rejected" if error.status_code in {401, 403}
                    else "rate_limited" if error.status_code == 429
                    else "provider_unavailable"
                )
                return {"id": name, "verified": False, "status": status}
            except (CapabilityUnavailableError, InvalidInputError):
                return {"id": name, "verified": False, "status": "provider_unavailable"}
            except Exception:
                return {"id": name, "verified": False, "status": "provider_unavailable"}
            return {"id": name, "verified": True, "status": "reachable", "modelCount": models}
        finally:
            self._test_lock.release()


def _valid_key_safe(value: object) -> bool:
    try:
        _valid_key(value)
        return True
    except InvalidInputError:
        return False


async def _document(request: Request, *, max_bytes: int = 6144) -> dict[str, Any]:
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise InvalidInputError("Expected application/json")
    try:
        declared = int(request.headers.get("content-length") or "0")
    except ValueError:
        raise InvalidInputError("Invalid API setup content length") from None
    if declared < 0 or declared > max_bytes:
        raise InvalidInputError("API setup request is too large")
    chunks = bytearray()
    try:
        async with asyncio.timeout(5):
            async for part in request.stream():
                chunks.extend(part)
                if len(chunks) > max_bytes:
                    raise InvalidInputError("API setup request is too large")
    except (TimeoutError, UnicodeError):
        raise InvalidInputError("API setup request is invalid") from None

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(chunks.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeError):
        raise InvalidInputError("Invalid API setup document") from None
    if not isinstance(value, dict):
        raise InvalidInputError("Expected API setup object")
    return value


def register_api_setup_routes(
    app: FastAPI,
    database: Database,
    authenticate: Callable[[str | None], WorkspaceContext],
    allowed_origins: tuple[str, ...],
    credentials: ServiceCredentials,
    *,
    changed: Callable[[str], None] | None = None,
) -> ApiSetupCredentials:
    """Register opt-in setup bridge; normal workspace API access is insufficient.

    This is intended for an operator Next.js bridge on the loopback service.
    The browser never receives the setup capability. The service's existing
    sanitized error middleware handles unexpected provider/vault failures.
    """
    service = ApiSetupCredentials(credentials, changed=changed)

    def actor(request: Request, *, mutating: bool) -> WorkspaceContext:
        if request.query_params:
            raise InvalidInputError("API setup does not accept query credentials")
        origin = request.headers.get("origin")
        if (mutating and origin not in allowed_origins) or (
            origin is not None and origin not in allowed_origins
        ) or request.headers.get("sec-fetch-site") == "cross-site":
            raise ScopeDeniedError("Untrusted API setup origin")
        identity = authenticate(request.headers.get("authorization"))
        with database.transaction(identity):
            pass
        service.authorize_setup(request.headers.get("x-potbot-setup-token"))
        return identity

    def response(value: dict[str, Any]) -> JSONResponse:
        return JSONResponse(value, headers=_HEADERS)

    @app.get("/v1/ui/setup/apis")
    async def list_apis(request: Request) -> JSONResponse:
        actor(request, mutating=False)
        return response(await run_in_threadpool(service.catalog))

    @app.put("/v1/ui/setup/apis/{provider}")
    async def save_api(request: Request, provider: str) -> JSONResponse:
        actor(request, mutating=True)
        name = _provider(provider)
        body = await _document(request)
        if set(body) != {"key", "confirm", "replace"}:
            raise InvalidInputError("Unexpected API credential fields")
        return response(await run_in_threadpool(
            service.put, name, body["key"], confirm=body["confirm"], replace=body["replace"]
        ))

    @app.delete("/v1/ui/setup/apis/{provider}")
    async def remove_api(request: Request, provider: str) -> JSONResponse:
        actor(request, mutating=True)
        name = _provider(provider)
        body = await _document(request)
        if set(body) != {"confirm"}:
            raise InvalidInputError("Unexpected API credential fields")
        return response(await run_in_threadpool(service.delete, name, confirm=body["confirm"]))

    @app.post("/v1/ui/setup/apis/{provider}/test")
    async def test_api(request: Request, provider: str) -> JSONResponse:
        actor(request, mutating=True)
        name = _provider(provider)
        body = await _document(request)
        if set(body) != {"confirm"} or body["confirm"] is not True:
            raise InvalidInputError("Explicit metadata-only provider test confirmation required")
        return response(await run_in_threadpool(service.test, name))

    return service
