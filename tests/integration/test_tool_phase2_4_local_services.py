"""Real loopback GraphQL-over-HTTP and gRPC fixtures. No public network traffic."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import httpx
import pytest

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools.api_contracts import ApiCallScope, ApiOperation
from potbot.tools.api_grpc import (
    ApprovedGrpcAdapter,
    InstalledGrpcRead,
    grpc_read_configuration_hash,
    protobuf_struct_codec,
)
from potbot.tools.api_http import ApprovedHttpApiAdapter
from potbot.tools.capability_contracts import (
    ToolImplementationDescriptor,
    ToolInstallationDescriptor,
    ToolInvocationIntent,
)


@pytest.fixture
def pinned_grpc_provider(monkeypatch):
    """Run real TLS and protobuf locally while intercepting the checked numeric dial."""
    import grpc
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    host = "grpc.fixture.example"
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
    ).public_bytes(serialization.Encoding.PEM)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    codec = protobuf_struct_codec("a" * 64)
    calls = []
    addresses = ["93.184.215.14"]
    dials = []

    def lookup(request, context):
        argument = codec.deserialize(request)
        calls.append(argument)
        if argument["id"] == "provider-lost":
            context.abort(grpc.StatusCode.UNAVAILABLE, "fixture discarded the response")
        return codec.serialize({"value": "hello:" + argument["id"]})

    connect = grpc.aio.secure_channel
    tls = grpc.ssl_channel_credentials
    resolver = socket.getaddrinfo

    def resolve(name, port, **kwargs):
        if name not in {host, "wrong.fixture.example"}:
            return resolver(name, port, **kwargs)
        assert port == 443 and kwargs["family"] == socket.AF_INET
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in addresses]

    with ThreadPoolExecutor(max_workers=2) as workers:
        server = grpc.server(workers)
        server.add_generic_rpc_handlers(
            (
                grpc.method_handlers_generic_handler(
                    "sample.Registry", {"Lookup": grpc.unary_unary_rpc_method_handler(lookup)}
                ),
            )
        )
        port = server.add_secure_port("127.0.0.1:0", grpc.ssl_server_credentials(((pem, cert),)))
        assert port
        server.start()

        def local_channel(target, credentials, *, options):
            assert target == "ipv4:93.184.215.14:443"
            opts = dict(options)
            assert opts["grpc.enable_retries"] == 0 and opts["grpc.enable_http_proxy"] == 0
            dials.append(opts["grpc.default_authority"])
            return connect(f"ipv4:127.0.0.1:{port}", credentials, options=options)

        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        monkeypatch.setattr(grpc.aio, "secure_channel", local_channel)
        monkeypatch.setattr(grpc, "ssl_channel_credentials", lambda: tls(root_certificates=cert))
        try:
            yield host, codec, calls, dials, addresses
        finally:
            assert server.stop(0).wait(5)


def scope_for(
    op: ApiOperation, args: dict[str, object], *, approve=None, egress=None
) -> ApiCallScope:
    w = uuid4()
    installation = ToolInstallationDescriptor(
        installation_ref="syn://thing/test-api-installation",
        workspace_id=w,
        implementation_hash="b" * 64,
        configuration_hash="c" * 64,
        policy_revision_hash="d" * 64,
        permitted_scopes=("read",),
    )
    intent = ToolInvocationIntent(
        invocation_id=uuid4(),
        workspace_id=w,
        task_revision_id=uuid4(),
        installation_hash=installation.fingerprint,
        input_type_ref=op.input_type_ref,
        input_hash=content_hash(args),
        effect_class=op.effect_class,
        policy_revision_hash=installation.policy_revision_hash,
        max_wall_time_ms=op.timeout_ms,
        max_output_bytes=op.max_output_bytes,
        idempotency_key="api-test-001",
    )
    return ApiCallScope(
        installation,
        intent,
        operation_hash=op.fingerprint,
        authorize=approve or (lambda *_: None),
        egress=egress or (lambda *_: None),
    )


def test_local_graphql_http_server_round_trip() -> None:
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            requests.append(data)
            body = json.dumps({"data": {"lookup": {"id": data["variables"]["id"]}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/graphql-response+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        op = ApiOperation(
            name="lookup",
            protocol="graphql",
            effect_class="read_only",
            target="/graphql",
            method="POST",
            graphql_document="query lookup($id: ID!) { lookup(id:$id) { id } }",
            source_hash="a" * 64,
            input_schema_json=canonical_json(
                {
                    "type": "object",
                    "properties": {
                        "variables": {
                            "type": "object",
                            "properties": {"id": {"type": "string"}},
                            "required": ["id"],
                            "additionalProperties": False,
                        }
                    },
                    "required": ["variables"],
                    "additionalProperties": False,
                }
            ),
            output_schema_json=canonical_json(
                {
                    "type": "object",
                    "properties": {
                        "lookup": {
                            "type": "object",
                            "properties": {"id": {"type": "string"}},
                            "required": ["id"],
                        }
                    },
                    "required": ["lookup"],
                }
            ),
        )
        args = {"variables": {"id": "abc"}}

        async def check():
            async with httpx.AsyncClient(trust_env=False) as client:
                return await ApprovedHttpApiAdapter(
                    origin=f"http://127.0.0.1:{port}", client=client, test_loopback=True
                ).call(op, args, scope_for(op, args))

        result = asyncio.run(check())
        assert result.status == "succeeded" and result.value == {"lookup": {"id": "abc"}}
        assert requests[0]["query"] == op.graphql_document
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_installed_grpc_dials_the_checked_address_and_verifies_the_named_peer(pinned_grpc_provider):
    host, codec, calls, dials, addresses = pinned_grpc_provider
    schema = canonical_json({"type": "object"})
    operation = ApiOperation(
        "lookup",
        "grpc",
        "read_only",
        "/sample.Registry/Lookup",
        "UNARY",
        schema,
        schema,
        codec.descriptor_hash,
        timeout_ms=2000,
        max_output_bytes=1024,
    )
    args = {"id": "sample"}
    scope = scope_for(operation, args)
    actor = WorkspaceContext(scope.installation.workspace_id, uuid4())
    implementation = ToolImplementationDescriptor(
        "syn://thing/grpc-test", operation.to_capability().fingerprint, "a" * 64, "Fixture", "1"
    )

    def installed(name):
        installation = replace(
            scope.installation,
            implementation_hash=implementation.fingerprint,
            configuration_hash=grpc_read_configuration_hash(actor, name, operation),
        )
        client = InstalledGrpcRead(actor, name, operation, codec, implementation, installation)
        return client, replace(
            scope,
            installation=installation,
            egress=None,
            intent=replace(scope.intent, installation_hash=installation.fingerprint),
        )

    async def exercise():
        client, admission = installed(host)
        result = await client.call(actor, args, admission)
        assert result.value == {"value": "hello:sample"} and result.attempts == 1
        other, other_scope = installed("wrong.fixture.example")
        mismatch = await other.call(actor, args, other_scope)
        assert mismatch.error_code == "grpc_unavailable"
        assert calls == [args] and dials == [host, "wrong.fixture.example"]
        with pytest.raises(ScopeDeniedError, match="pinned installation"):
            replace(client, operation=replace(operation, target="/sample.Registry/Other"))
        with pytest.raises(ScopeDeniedError, match="pinned installation"):
            replace(client, codec=replace(codec, descriptor_hash="b" * 64))
        with pytest.raises(ScopeDeniedError, match="arguments"):
            await client.call(actor, {"id": "changed"}, admission)
        addresses.append("127.0.0.1")
        with pytest.raises(ScopeDeniedError, match="private or special"):
            await client.call(actor, args, admission)
        assert calls == [args] and dials == [host, "wrong.fixture.example"]

    asyncio.run(exercise())


def test_local_grpc_generated_wire_contract() -> None:
    grpc = pytest.importorskip("grpc")

    async def check():
        from google.protobuf import json_format
        from google.protobuf.struct_pb2 import Struct

        async def handler(request_bytes, context):
            m = Struct()
            m.ParseFromString(request_bytes)
            data = json_format.MessageToDict(m)
            response = Struct()
            response.update({"value": "hello:" + data["id"]})
            return response.SerializeToString(deterministic=True)

        server = grpc.aio.server()
        server.add_generic_rpc_handlers(
            (
                grpc.method_handlers_generic_handler(
                    "sample.Registry",
                    {
                        "Lookup": grpc.unary_unary_rpc_method_handler(
                            handler,
                            request_deserializer=lambda b: b,
                            response_serializer=lambda b: b,
                        )
                    },
                ),
            )
        )
        port = server.add_insecure_port("127.0.0.1:0")
        assert port > 0
        await server.start()
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        try:
            op = ApiOperation(
                name="lookup",
                protocol="grpc",
                effect_class="read_only",
                method="UNARY",
                target="/sample.Registry/Lookup",
                source_hash="a" * 64,
                input_schema_json=canonical_json(
                    {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                        "required": ["id"],
                        "additionalProperties": False,
                    }
                ),
                output_schema_json=canonical_json(
                    {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    }
                ),
            )
            args = {"id": "x"}
            result = await ApprovedGrpcAdapter(
                channel=channel,
                installed_codecs={op.target: protobuf_struct_codec(op.source_hash)},
                channel_approval=lambda _: None,
            ).call(op, args, scope_for(op, args))
            assert result.status == "succeeded" and result.value == {"value": "hello:x"}
        finally:
            await channel.close()
            await server.stop(grace=0)

    asyncio.run(check())
