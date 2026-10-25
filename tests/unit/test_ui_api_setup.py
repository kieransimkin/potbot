"""S8: operator-only provider credential setup, with no secret disclosure."""

from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from potbot.api.ui_api_setup import ApiSetupCredentials, register_api_setup_routes
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import CapabilityUnavailableError, DomainError, ScopeDeniedError

SETUP = "private-setup-" + "q" * 48
WORKSPACE_TOKEN = "workspace-access-" + "w" * 48
ORIGIN = "http://localhost:3100"


class MemoryNativeVault:
    profile = "operator-fixture"

    def __init__(self):
        self.items = {"api.setup_token": SETUP}
        self.locked = False

    def get(self, name):
        if self.locked:
            raise CapabilityUnavailableError("unavailable")
        return self.items.get(name)

    def set(self, name, value):
        if self.locked:
            raise CapabilityUnavailableError("unavailable")
        self.items[name] = value
        assert self.items[name] == value

    def delete(self, name):
        if self.locked:
            raise CapabilityUnavailableError("unavailable")
        return self.items.pop(name, None) is not None


class Credentials:
    def __init__(self):
        self.environment = {}
        self.store = MemoryNativeVault()

    def resolve(self, reference):
        name = reference.removeprefix("secret://providers/")
        from potbot.service_credentials import PROVIDER_ENVIRONMENT
        return self.environment.get(PROVIDER_ENVIRONMENT[name]) or self.store.get("providers." + name)


def setup_app():
    credentials = Credentials()
    database_calls = []
    identity = WorkspaceContext(uuid4(), uuid4())

    class Database:
        @contextmanager
        def transaction(self, context):
            database_calls.append(context)
            assert context == identity
            yield None

    def authenticate(value):
        if value != "Bearer " + WORKSPACE_TOKEN:
            raise ScopeDeniedError("Workspace access denied")
        return identity

    app = FastAPI()

    @app.exception_handler(DomainError)
    async def domain_errors(_request, error):
        return JSONResponse({"error": error.code.value}, status_code=403)

    service = register_api_setup_routes(app, Database(), authenticate, (ORIGIN,), credentials)
    return TestClient(app), service, credentials, database_calls


def headers(*, admin=True, origin=True, workspace=True):
    value = {"authorization": "Bearer " + (WORKSPACE_TOKEN if workspace else "invalid-token")}
    if admin:
        value["x-potbot-setup-token"] = SETUP
    if origin:
        value["origin"] = ORIGIN
    return value


def test_setup_catalog_is_status_only_and_all_five_builtin_providers():
    client, _service, credentials, calls = setup_app()
    credentials.store.set("providers.openai", "vault-secret-123")
    res = client.get("/v1/ui/setup/apis", headers=headers())
    assert res.status_code == 200
    data = res.json()
    assert data["schema"] == "potbot-api-setup/v1"
    assert data["secretsExposed"] is False
    assert {p["id"] for p in data["providers"]} == {"openai", "anthropic", "gemini", "xai", "evolink"}
    openai = next(p for p in data["providers"] if p["id"] == "openai")
    assert openai["configured"] and openai["source"] == "native_vault"
    assert "vault-secret-123" not in res.text and SETUP not in res.text
    assert calls


def test_user_workspace_bearer_does_not_grant_vault_read_write_or_delete():
    client, _service, credentials, _ = setup_app()
    for method, path, body in [
        ("get", "/v1/ui/setup/apis", None),
        ("put", "/v1/ui/setup/apis/openai", {"key":"secret-123", "confirm":True, "replace":False}),
        ("delete", "/v1/ui/setup/apis/openai", {"confirm":True}),
    ]:
        res = client.request(method.upper(), path, headers=headers(admin=False), **({"json":body} if body else {}))
        assert res.status_code >= 400
    assert credentials.store.get("providers.openai") is None
    assert client.get("/v1/ui/setup/apis", headers=headers(workspace=False)).status_code >= 400
    assert client.get("/v1/ui/setup/apis", headers={**headers(),"x-potbot-setup-token":"wrong"}).status_code >= 400


def test_exact_provider_allowlist_and_no_query_credentials():
    client, _service, _, _ = setup_app()
    assert client.get("/v1/ui/setup/apis?key=sensitive", headers=headers()).status_code >= 400
    res = client.put("/v1/ui/setup/apis/unknown", headers=headers(), json={"key":"value", "confirm":True,"replace":False})
    assert res.status_code >= 400
    assert "value" not in res.text


def test_provider_create_rotate_and_delete_never_return_key():
    client, _, credentials, _ = setup_app()
    create = client.put("/v1/ui/setup/apis/gemini", headers=headers(), json={"key":"new-key", "confirm":True,"replace":False})
    assert create.status_code == 200 and create.json()["configured"]
    assert credentials.store.get("providers.gemini") == "new-key"
    assert "new-key" not in create.text
    conflict = client.put("/v1/ui/setup/apis/gemini", headers=headers(), json={"key":"newest-key", "confirm":True,"replace":False})
    assert conflict.status_code >= 400
    rotated = client.put("/v1/ui/setup/apis/gemini", headers=headers(), json={"key":"newest-key", "confirm":True,"replace":True})
    assert rotated.status_code == 200 and credentials.store.get("providers.gemini") == "newest-key"
    removed = client.request("DELETE", "/v1/ui/setup/apis/gemini", headers=headers(), json={"confirm":True})
    assert removed.status_code == 200 and removed.json()["deleted"]
    assert credentials.store.get("providers.gemini") is None


@pytest.mark.parametrize("value", ["", "contains space", "line\nbreak", "\x00", "ä", "x" * 4097])
def test_invalid_key_rejected_without_save(value):
    client, _, credentials, _ = setup_app()
    res = client.put("/v1/ui/setup/apis/openai", headers=headers(), json={"key":value, "confirm":True,"replace":False})
    assert res.status_code >= 400
    assert credentials.store.get("providers.openai") is None
    assert value not in res.text if value else True


def test_environment_override_prevents_ineffective_edits():
    client, _, credentials, _ = setup_app()
    credentials.environment["OPENAI_API_KEY"] = "env-overrides-native"
    status = client.get("/v1/ui/setup/apis", headers=headers()).json()
    obj = next(p for p in status["providers"] if p["id"] == "openai")
    assert obj["source"] == "environment" and not obj["editable"]
    assert client.put("/v1/ui/setup/apis/openai", headers=headers(), json={"key":"new-key","confirm":True,"replace":True}).status_code >= 400
    assert client.request("DELETE", "/v1/ui/setup/apis/openai", headers=headers(), json={"confirm":True}).status_code >= 400
    assert credentials.store.get("providers.openai") is None


def test_origin_rejected_and_duplicate_json_key_rejected():
    client, _, credentials, _ = setup_app()
    assert client.put("/v1/ui/setup/apis/xai", headers=headers(origin=False), json={"key":"new-key","confirm":True,"replace":False}).status_code >= 400
    assert client.put("/v1/ui/setup/apis/xai", headers={**headers(),"origin":"https://evil.example"}, json={"key":"new-key","confirm":True,"replace":False}).status_code >= 400
    raw = b'{"key":"first","key":"second","confirm":true,"replace":false}'
    res = client.put("/v1/ui/setup/apis/xai", headers={**headers(),"content-type":"application/json"}, content=raw)
    assert res.status_code >= 400 and credentials.store.get("providers.xai") is None


def test_locked_vault_is_safe_and_setup_unavailable():
    client, _, creds, _ = setup_app()
    creds.store.locked = True
    res = client.get("/v1/ui/setup/apis", headers=headers())
    assert res.status_code >= 400
    assert SETUP not in res.text


def test_metadata_probe_only_when_confirmed_and_never_returns_response_secrets():
    client, service, credentials, _ = setup_app()
    credentials.store.set("providers.anthropic", "secret-probe")
    calls = []

    def probe(_creds, provider):
        calls.append(provider)
        return 13

    service.probe = probe
    res = client.post("/v1/ui/setup/apis/anthropic/test", headers=headers(), json={"confirm":True})
    assert res.status_code == 200 and res.json()["modelCount"] == 13
    assert calls == ["anthropic"]
    assert client.post("/v1/ui/setup/apis/anthropic/test", headers=headers(), json={"confirm":False}).status_code >= 400
    assert "secret-probe" not in res.text


def test_put_payload_size_is_bounded_with_no_partial_write():
    client, _, credentials, _ = setup_app()
    res = client.put("/v1/ui/setup/apis/openai", headers=headers(), json={"key":"a"*7000,"confirm":True,"replace":False})
    assert res.status_code >= 400
    assert credentials.store.get("providers.openai") is None
