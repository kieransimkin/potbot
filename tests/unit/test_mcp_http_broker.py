"""Defensive tests for the controller-owned MCP HTTP boundary."""

import asyncio
import json
import socket
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.tools.mcp_http_broker import McpHttpBroker, McpHttpGrant, ScopedMcpBearer

RESOURCE = "https://mcp.fixture.example/mcp"
ISSUER = "https://login.fixture.example/tenant"
PUBLIC = "93.184.215.14"
TOKEN = "fixture-token-for-mcp"
MESSAGE = {"jsonrpc": "2.0", "id": 1, "method": "server/discover"}


@pytest.fixture
def grant():
    return McpHttpGrant(uuid4(), uuid4(), RESOURCE, ISSUER, ("tools:read",))


@pytest.fixture
def wire(monkeypatch, grant):
    addresses = [PUBLIC]
    requests = []
    dns = []
    metadata = {
        "issuer": ISSUER,
        "authorization_endpoint": ISSUER + "/authorize",
        "token_endpoint": ISSUER + "/token",
        "jwks_uri": ISSUER + "/keys",
    }
    response = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}

    def resolve(host, port, *, family, type, proto):
        dns.append(host)
        assert (family, type, proto, port) == (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            443,
        )
        return [(family, type, proto, "", (ip, port)) for ip in addresses]

    async def handle(self, request):
        requests.append(request)
        assert request.url.host == PUBLIC
        assert request.headers["host"] == request.extensions["sni_hostname"]
        assert "cookie" not in request.headers
        assert request.headers["accept-encoding"] == "identity"
        if request.method == "GET":
            assert request.url.path == httpx.URL(grant.discovery).path
            assert request.headers["host"] == "login.fixture.example"
            assert "authorization" not in request.headers
            return httpx.Response(200, json=metadata, headers={"set-cookie": "bad=1"})
        assert request.url.path == "/mcp"
        assert request.headers["host"] == "mcp.fixture.example"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        body = json.dumps(response).replace(TOKEN, TOKEN.replace("-", r"\u002d"))
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    return addresses, requests, dns, metadata, response


def bearer(grant):
    return ScopedMcpBearer(grant, RESOURCE, 2**31, TOKEN)


def test_exact_route_resolves_per_request_and_rechecks_consent(grant, wire):
    _, requests, dns, _, _ = wire
    allowed = True
    received = []

    def credential(scope):
        received.append(scope)
        return bearer(scope)

    async def scenario():
        nonlocal allowed
        broker = McpHttpBroker(lambda scope: scope == grant and allowed, credential)
        async with broker.open(grant) as transport:
            assert (await transport.exchange(MESSAGE, True))["result"] == {"tools": []}
            assert (await transport.exchange(MESSAGE, True))["result"] == {"tools": []}
            allowed = False
            with pytest.raises(ScopeDeniedError, match="explicit consent"):
                await transport.exchange(MESSAGE, True)

    asyncio.run(scenario())
    assert [request.method for request in requests] == ["GET", "POST", "POST"]
    assert dns == ["login.fixture.example", "mcp.fixture.example", "mcp.fixture.example"]
    assert received == [grant, grant]


@pytest.mark.parametrize("private", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "168.63.129.16"])
def test_mixed_dns_answer_refused_before_connect(grant, wire, private):
    addresses, requests, dns, _, _ = wire
    broker = McpHttpBroker(lambda _: True, bearer)

    async def scenario():
        async with broker.open(grant) as transport:
            addresses.append(private)
            with pytest.raises(ScopeDeniedError, match="private or special"):
                await transport.exchange(MESSAGE, True)

    asyncio.run(scenario())
    assert [request.method for request in requests] == ["GET"]
    assert dns == ["login.fixture.example", "mcp.fixture.example"]


@pytest.mark.parametrize(
    "difference", ["audience", "principal", "workspace", "scope", "issuer", "expiry"]
)
def test_credential_bound_to_exact_grant(grant, wire, difference):
    _, requests, dns, _, _ = wire
    original = bearer(grant)
    wrong = {
        "audience": replace(original, audience=RESOURCE + "/other"),
        "principal": replace(original, grant=replace(grant, principal_id=uuid4())),
        "workspace": replace(original, grant=replace(grant, workspace_id=uuid4())),
        "scope": replace(original, grant=replace(grant, scopes=("tools:write",))),
        "issuer": replace(original, grant=replace(grant, issuer=ISSUER + "/other")),
        "expiry": replace(original, expires_at=1),
    }[difference]
    assert TOKEN not in repr(wrong)

    async def scenario():
        async with McpHttpBroker(lambda _: True, lambda _: wrong).open(grant) as transport:
            with pytest.raises(ScopeDeniedError, match="approved resource"):
                await transport.exchange(MESSAGE, True)

    asyncio.run(scenario())
    assert [request.method for request in requests] == ["GET"]
    assert dns == ["login.fixture.example"]


def test_wrong_issuer_stops_before_token_access(grant, wire):
    _, requests, _, metadata, _ = wire
    metadata["issuer"] = ISSUER + "/other"

    async def scenario():
        async with McpHttpBroker(lambda _: True, lambda _: pytest.fail("token accessed")).open(
            grant
        ):
            pytest.fail("mismatched issuer accepted")

    with pytest.raises(ScopeDeniedError, match="identity mismatch"):
        asyncio.run(scenario())
    assert [request.method for request in requests] == ["GET"]


def test_discovery_requires_consent_before_dns(grant, wire):
    _, requests, dns, _, _ = wire
    with pytest.raises(ScopeDeniedError, match="explicit consent"):
        asyncio.run(McpHttpBroker(lambda _: False, bearer).discover_issuer(grant))
    assert requests == dns == []


def test_reflected_bearer_does_not_become_an_observation(grant, wire):
    _, _, _, _, response = wire
    response["result"] = {"nested": [{f"Bearer {TOKEN}": "redact"}]}

    async def scenario():
        async with McpHttpBroker(lambda _: True, bearer).open(grant) as transport:
            with pytest.raises(ScopeDeniedError, match="authorization secret") as error:
                await transport.exchange(MESSAGE, True)
            assert TOKEN not in str(error.value)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "url",
    [
        RESOURCE + "?",
        RESOURCE + "#",
        RESOURCE.replace("/mcp", "/x/../mcp"),
        "https://127.0.0.1/mcp",
    ],
)
def test_route_aliases_refused(grant, url):
    with pytest.raises((InvalidInputError, ScopeDeniedError)):
        replace(grant, resource=url)


def test_pinned_socket_still_verifies_the_approved_hostname(tmp_path, monkeypatch):
    import ssl
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    from httpcore._backends.anyio import AnyIOBackend

    from potbot.domain.auth import WorkspaceContext
    from potbot.domain.serialization import content_hash
    from potbot.tools.api_contracts import ApiCallScope
    from potbot.tools.api_https import InstalledRestRead, rest_read_configuration_hash
    from potbot.tools.api_openapi import compile_openapi
    from potbot.tools.capability_contracts import (
        ToolImplementationDescriptor,
        ToolInstallationDescriptor,
        ToolInvocationIntent,
    )
    from potbot.tools.pinned_https import PinnedHttpsTransport

    host = "mcp.fixture.example"
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "tls.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(cert_file, key_file)
    client_tls = ssl.create_default_context(cafile=str(cert_file))
    accepted = []
    dialed = []
    original_init = httpx.AsyncHTTPTransport.__init__
    original_connect = AnyIOBackend.connect_tcp

    def configure(self, **kwargs):
        assert kwargs["trust_env"] is False and kwargs["retries"] == 0
        original_init(self, **kwargs, verify=client_tls)

    async def respond(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            accepted.append(headers)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: 12\r\nConnection: close\r\n\r\n"
                b'{"value":23}'
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def scenario():
        async with await asyncio.start_server(respond, "127.0.0.1", 0, ssl=server_tls) as server:
            assert server.sockets is not None
            port = server.sockets[0].getsockname()[1]

            async def connect(self, host, port, **kwargs):
                dialed.append((host, port))
                return await original_connect(self, "127.0.0.1", fixture_port, **kwargs)

            fixture_port = port
            monkeypatch.setattr(AnyIOBackend, "connect_tcp", connect)
            for endpoint in (RESOURCE, "https://other.fixture.example/mcp"):
                async with httpx.AsyncClient(
                    transport=PinnedHttpsTransport("POST", endpoint, authenticated=True),
                    trust_env=False,
                ) as client:
                    headers = {"authorization": f"Bearer {TOKEN}"}
                    if endpoint == RESOURCE:
                        assert (await client.post(endpoint, headers=headers)).status_code == 200
                    else:
                        with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                            await client.post(endpoint, headers=headers)

            origin = "https://mcp.fixture.example"
            endpoint = origin + "/items/selection?limit=2"
            async with httpx.AsyncClient(
                transport=PinnedHttpsTransport("GET", endpoint, allow_query=True),
                trust_env=False,
            ) as client:
                with pytest.raises(ScopeDeniedError):
                    await client.get(endpoint.replace("limit=2", "limit=3"))

            (op,) = compile_openapi(
                {
                    "openapi": "3.1.0",
                    "servers": [{"url": "https://unused.fixture.example"}],
                    "paths": {
                        "/items/{id}": {
                            "get": {
                                "operationId": "get_item",
                                "parameters": [
                                    {
                                        "in": "path",
                                        "name": "id",
                                        "required": True,
                                        "schema": {"type": "string"},
                                    },
                                    {"in": "query", "name": "limit", "schema": {"type": "integer"}},
                                ],
                                "responses": {
                                    "200": {
                                        "content": {
                                            "application/json": {
                                                "schema": {
                                                    "type": "object",
                                                    "properties": {"value": {"type": "integer"}},
                                                    "required": ["value"],
                                                    "additionalProperties": False,
                                                }
                                            }
                                        }
                                    }
                                },
                            }
                        },
                    },
                },
                selected_effects={"get_item": "read_only"},
            )
            actor = WorkspaceContext(uuid4(), uuid4())
            implementation = ToolImplementationDescriptor(
                "syn://thing/tls-rest-implementation",
                op.to_capability().fingerprint,
                "a" * 64,
                "Fixture publisher",
                "1",
            )
            installation = ToolInstallationDescriptor(
                "syn://thing/tls-rest-installation",
                actor.workspace_id,
                implementation.fingerprint,
                rest_read_configuration_hash(actor, origin, op),
                "b" * 64,
                ("read",),
            )
            args = {"path": {"id": "selection"}, "query": {"limit": 2}}
            intent = ToolInvocationIntent(
                uuid4(),
                actor.workspace_id,
                uuid4(),
                installation.fingerprint,
                op.input_type_ref,
                content_hash(args),
                "read_only",
                installation.policy_revision_hash,
                op.timeout_ms,
                op.max_output_bytes,
                "local-tls-rest",
            )
            scope = ApiCallScope(installation, intent, op.fingerprint, lambda *_: None)
            result = await InstalledRestRead(actor, origin, op, implementation, installation).call(
                actor,
                args,
                scope,
            )
            assert result.status == "succeeded" and result.value == {"value": 23}
            assert result.to_observation(intent, op).invocation_hash == intent.fingerprint

    def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC, port))]

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "__init__", configure)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    asyncio.run(scenario())
    assert dialed == [(PUBLIC, 443)] * 3
    assert len(accepted) == 2
    assert b"Host: mcp.fixture.example\r\n" in accepted[0]
    assert f"authorization: Bearer {TOKEN}\r\n".encode() in accepted[0]
    assert accepted[1].startswith(b"GET /items/selection?limit=2 HTTP/1.1\r\n")
    assert b"authorization:" not in accepted[1].lower()
