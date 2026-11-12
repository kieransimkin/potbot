"""Disposable OAuth issuer and native-vault contract tests."""

import asyncio
import base64
import hashlib
import json
import socket
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from potbot.api.hosted_mcp_oauth import HostedMcpOAuth
from potbot.api.server import create_app
from potbot.credentials import CredentialStore
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError
from potbot.tools.mcp_http_broker import McpHttpGrant
from potbot.tools.mcp_oauth import McpOAuthProvider, McpOAuthRegistration

ISSUER = "https://login.fixture.example/tenant"
RESOURCE = "https://mcp.fixture.example/tools"
CALLBACK = "https://controller.fixture.example/v1/mcp/oauth/callback"
PUBLIC = "93.184.215.14"
CODE = "one-use-fixture-code"


class VaultFixture:
    def __init__(self):
        self.values = {}

    def get_password(self, service, name):
        return self.values.get((service, name))

    def set_password(self, service, name, value):
        self.values[service, name] = value

    def delete_password(self, service, name):
        del self.values[service, name]


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def rig(monkeypatch, signing_key):
    grant = McpHttpGrant(uuid4(), uuid4(), RESOURCE, ISSUER, ("tools:read",))
    registration = McpOAuthRegistration(
        "fixture-client",
        "fixture-subject",
        CALLBACK,
        ISSUER + "/authorize",
        ISSUER + "/token",
        "fixture-key",
        signing_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ),
    )
    consent = [True]
    vault = VaultFixture()
    store = CredentialStore(backend=vault)
    provider = McpOAuthProvider(grant, registration, lambda g: g == grant and consent[0], store)
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": registration.subject,
        "client_id": registration.client_id,
        "aud": RESOURCE,
        "scope": "tools:read",
        "iat": now - 1,
        "exp": now + 600,
        "jti": "fixture-token",
    }
    header = {"typ": "at+jwt", "alg": "RS256", "kid": registration.key_id}
    calls = []
    form = []
    auth = []
    metadata = {
        "issuer": ISSUER,
        "token_endpoint": registration.token_endpoint,
        "authorization_endpoint": registration.authorization_endpoint,
        "jwks_uri": ISSUER + "/keys",
        "code_challenge_methods_supported": ["S256"],
        "authorization_response_iss_parameter_supported": True,
    }
    behavior = {}

    def jwt():
        def b64(raw):
            return base64.urlsafe_b64encode(raw).rstrip(b"=")

        message = b".".join(b64(json.dumps(value).encode()) for value in (header, claims))
        signature = signing_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
        if behavior.get("bad_signature"):
            signature = bytes([signature[0] ^ 1]) + signature[1:]
        return (message + b"." + b64(signature)).decode()

    def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC, port))]

    async def wire(self, request):
        calls.append((request.method, request.headers["host"], request.url.path))
        assert request.url.host == PUBLIC
        assert request.extensions["sni_hostname"] == request.headers["host"]
        if request.method == "GET":
            assert "authorization" not in request.headers
            return httpx.Response(200, json=metadata)
        if request.url.path == "/tenant/token":
            assert "authorization" not in request.headers
            form.append(parse_qs((await request.aread()).decode()))
            if behavior.get("revoke_during_exchange"):
                consent[0] = False
            if behavior.get("redirect_token"):
                return httpx.Response(307, headers={"location": ISSUER + "/other"})
            return httpx.Response(
                200, json={"token_type": "Bearer", "access_token": jwt(), "refresh_token": "unused"}
            )
        assert request.url.path == "/tools"
        auth.append(request.headers["authorization"])
        request_body = json.loads(await request.aread())
        result = {"resultType": "complete", "supportedVersions": ["2026-07-28"]}
        if request_body["method"] == "tools/list":
            result = {"resultType": "complete", "tools": []}
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": request_body["id"], "result": result}
        )

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", wire)
    return provider, vault, claims, header, calls, form, auth, metadata, behavior, consent


def callback(start, **overrides):
    values = {
        "code": CODE,
        "state": parse_qs(urlsplit(start).query)["state"][0],
        "iss": ISSUER,
        **overrides,
    }
    return CALLBACK + "?" + urlencode(values)


def test_pkce_exchange_vault_readback_and_mcp_delivery(rig):
    provider, vault, _, _, calls, form, auth, _, _, _ = rig

    async def scenario():
        start = await provider.begin()
        params = parse_qs(urlsplit(start).query)
        assert params["resource"] == [RESOURCE]
        assert params["redirect_uri"] == [CALLBACK]
        assert params["code_challenge_method"] == ["S256"]
        returned = callback(start)
        await provider.complete(returned)
        posted = form[0]
        verifier = posted["code_verifier"][0]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        assert params["code_challenge"] == [challenge.rstrip(b"=").decode()]
        assert "code_verifier" not in params
        assert posted["resource"] == [RESOURCE]
        assert posted["code"] == [CODE]
        assert posted["grant_type"] == ["authorization_code"]
        assert posted["redirect_uri"] == [CALLBACK]
        assert posted["client_id"] == [provider.registration.client_id]
        with pytest.raises(ScopeDeniedError, match="no live attempt"):
            await provider.complete(returned)

        reopened = McpOAuthProvider(
            provider.grant, provider.registration, lambda _: True, provider.store
        )
        token = reopened(provider.grant)
        assert token.secret not in repr(token)
        assert list(vault.values.values()) == [token.secret]
        async with reopened.broker.open(provider.grant) as transport:
            result = await transport.exchange(
                {"jsonrpc": "2.0", "id": 1, "method": "server/discover"}, True
            )
        assert result["result"]["supportedVersions"] == ["2026-07-28"]
        assert auth == ["Bearer " + token.secret]
        assert await reopened.forget()
        assert not vault.values
        with pytest.raises(ScopeDeniedError, match="login required"):
            reopened(provider.grant)

    asyncio.run(scenario())
    assert [path for _, _, path in calls] == [
        "/tenant/.well-known/openid-configuration",
        "/tenant/token",
        "/tenant/.well-known/openid-configuration",
        "/tools",
    ]


@pytest.mark.parametrize("fault", ["state", "issuer", "callback", "duplicate"])
def test_invalid_callback_never_posts_a_code(rig, fault):
    provider, vault, _, _, calls, _, _, _, _, _ = rig

    async def scenario():
        start = await provider.begin()
        target = {
            "state": callback(start, state="wrong"),
            "issuer": callback(start, iss=ISSUER + "/other"),
            "callback": callback(start).replace(CALLBACK, CALLBACK + "/other"),
            "duplicate": callback(start) + "&code=second",
        }[fault]
        with pytest.raises(ScopeDeniedError, match="callback is invalid") as error:
            await provider.complete(target)
        assert CODE not in str(error.value)

    asyncio.run(scenario())
    assert len(calls) == 1
    assert vault.values == {}


@pytest.mark.parametrize(
    "claim, value",
    [
        ("aud", [RESOURCE, "https://another.fixture.example/mcp"]),
        ("scope", "tools:read tools:write"),
        ("iss", ISSUER + "/other"),
        ("sub", "other-subject"),
        ("client_id", "other-client"),
        ("exp", 0),
        ("nbf", 2**31),
        ("cnf", {"jkt": "unsupported"}),
    ],
)
def test_signed_but_wrong_grant_is_never_saved(rig, claim, value):
    provider, vault, claims, _, _, _, _, _, _, _ = rig
    claims[claim] = value

    async def scenario():
        with pytest.raises(ScopeDeniedError, match="failed verification"):
            await provider.complete(callback(await provider.begin()))

    asyncio.run(scenario())
    assert vault.values == {}


@pytest.mark.parametrize(
    "fault", ["bad_signature", "wrong_type", "redirect_token", "revoke_during_exchange"]
)
def test_exchange_fails_closed_without_saving_or_retrying(rig, fault):
    provider, vault, _, header, calls, _, _, _, behavior, _ = rig
    behavior[fault] = True
    if fault == "wrong_type":
        header["typ"] = "JWT"

    async def scenario():
        returned = callback(await provider.begin())
        with pytest.raises(ScopeDeniedError):
            await provider.complete(returned)
        with pytest.raises(ScopeDeniedError):
            await provider.complete(returned)

    asyncio.run(scenario())
    assert len(calls) == 2
    assert vault.values == {}


def test_metadata_endpoint_change_blocks_login(rig):
    provider, _, _, _, calls, _, _, metadata, _, _ = rig
    metadata["token_endpoint"] = ISSUER + "/unapproved"
    with pytest.raises(ScopeDeniedError, match="differ from the registration"):
        asyncio.run(provider.begin())
    assert len(calls) == 1


def test_vault_lookup_rechecks_consent_and_grant(rig):
    provider, _, _, _, _, _, _, _, _, consent = rig
    with pytest.raises(ScopeDeniedError, match="different grant"):
        provider(replace(provider.grant, workspace_id=uuid4()))
    with pytest.raises(ScopeDeniedError, match="login required"):
        provider(provider.grant)
    consent[0] = False
    with pytest.raises(ScopeDeniedError, match="explicit consent"):
        provider(provider.grant)


def test_hosted_oauth_requires_operator_and_consumes_only_exact_callback(rig):
    provider, vault, _, _, _, form, auth, _, _, _ = rig
    actor = WorkspaceContext(provider.grant.workspace_id, provider.grant.principal_id)

    def authenticate(value):
        if value != "Bearer workspace-fixture":
            raise ScopeDeniedError("unavailable")
        return actor

    def setup(value):
        if value != "fixture-operator":
            raise ScopeDeniedError("unavailable")

    app = create_app(
        task_service=object(),
        job_service=object(),
        authenticate=authenticate,
        mcp_oauth=HostedMcpOAuth(provider, setup),
    )
    workspace = {"Authorization": "Bearer workspace-fixture"}
    operator = {**workspace, "X-Potbot-Setup-Token": "fixture-operator"}

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://controller.fixture.example"
        ) as client:
            assert (await client.post("/v1/mcp/oauth/start", headers=workspace)).is_error
            start = await client.post("/v1/mcp/oauth/start", headers=operator)
            assert start.status_code == 200 and start.headers["cache-control"] == "no-store"
            target = callback(start.json()["authorization_url"])
            wrong = target.replace("controller.fixture.example", "other.fixture.example")
            refused = await client.get(
                wrong, headers={"X-Forwarded-Host": "controller.fixture.example"}
            )
            assert refused.status_code == 400 and CODE not in refused.text
            assert form == []
            done = await client.get(target)
            assert done.status_code == 200
            assert done.headers["referrer-policy"] == "no-referrer"
            assert CODE not in done.text and done.headers["cache-control"] == "no-store"
            assert (await client.get(target)).status_code == 400
            assert len(form) == 1
            assert (await client.post("/v1/mcp/oauth/check", headers=workspace)).is_error
            checked = await client.post("/v1/mcp/oauth/check", headers=operator)
            assert checked.status_code == 200 and checked.json()["protocol"] == "2026-07-28"
            assert checked.json()["tool_count"] == 0
            assert len(auth) == 2 and len(vault.values) == 1

            endpoint = "/v1/mcp/oauth/disconnect"
            assert (await client.post(endpoint, headers=workspace)).is_error
            assert len(vault.values) == 1
            retry = await client.post("/v1/mcp/oauth/start", headers=operator)
            retry_target = callback(retry.json()["authorization_url"])
            removed = await client.post(endpoint, headers=operator)
            assert removed.status_code == 204 and removed.content == b""
            assert removed.headers["cache-control"] == "no-store"
            assert not vault.values
            assert (await client.get(retry_target)).status_code == 400
            assert (await client.post("/v1/mcp/oauth/check", headers=operator)).is_error
            assert (await client.post(endpoint, headers=operator)).status_code == 204
            assert len(form) == 1 and len(auth) == 2

    asyncio.run(scenario())
