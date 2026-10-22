"""Real loopback GraphQL-over-HTTP and gRPC fixtures. No public network traffic."""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import httpx
import pytest

from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools.api_contracts import ApiCallScope, ApiOperation
from potbot.tools.api_grpc import ApprovedGrpcAdapter, protobuf_struct_codec
from potbot.tools.api_http import ApprovedHttpApiAdapter
from potbot.tools.capability_contracts import ToolInstallationDescriptor, ToolInvocationIntent


def scope_for(op: ApiOperation, args: dict[str, object], *, approve=None, egress=None) -> ApiCallScope:
    w = uuid4()
    installation = ToolInstallationDescriptor(
        installation_ref="syn://thing/test-api-installation", workspace_id=w,
        implementation_hash="b"*64, configuration_hash="c"*64,
        policy_revision_hash="d"*64, permitted_scopes=("read",),
    )
    intent = ToolInvocationIntent(
        invocation_id=uuid4(), workspace_id=w, task_revision_id=uuid4(),
        installation_hash=installation.fingerprint, input_type_ref=op.input_type_ref,
        input_hash=content_hash(args), effect_class=op.effect_class,
        policy_revision_hash=installation.policy_revision_hash,
        max_wall_time_ms=op.timeout_ms, max_output_bytes=op.max_output_bytes,
        idempotency_key="api-test-001",
    )
    return ApiCallScope(installation, intent, operation_hash=op.fingerprint, authorize=approve or (lambda *_: None),
                        egress=egress or (lambda *_: None))





def test_local_graphql_http_server_round_trip() -> None:
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length=int(self.headers.get("Content-Length","0"))
            data=json.loads(self.rfile.read(length))
            requests.append(data)
            body=json.dumps({"data":{"lookup":{"id":data["variables"]["id"]}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type","application/graphql-response+json")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self,*args):
            pass
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        port=server.server_address[1]
        op=ApiOperation(name="lookup",protocol="graphql",effect_class="read_only",target="/graphql",method="POST",
            graphql_document="query lookup($id: ID!) { lookup(id:$id) { id } }",source_hash="a"*64,
            input_schema_json=canonical_json({"type":"object","properties":{"variables":{"type":"object","properties":{"id":{"type":"string"}},"required":["id"],"additionalProperties":False}},"required":["variables"],"additionalProperties":False}),
            output_schema_json=canonical_json({"type":"object","properties":{"lookup":{"type":"object","properties":{"id":{"type":"string"}},"required":["id"]}},"required":["lookup"]}))
        args={"variables":{"id":"abc"}}
        async def check():
            async with httpx.AsyncClient(trust_env=False) as client:
                return await ApprovedHttpApiAdapter(origin=f"http://127.0.0.1:{port}",client=client,test_loopback=True).call(op,args,scope_for(op,args))
        result=asyncio.run(check())
        assert result.status=="succeeded" and result.value=={"lookup":{"id":"abc"}}
        assert requests[0]["query"]==op.graphql_document
    finally:
        server.shutdown();server.server_close();thread.join(timeout=1)


def test_local_grpc_generated_wire_contract() -> None:
    grpc=pytest.importorskip("grpc")
    async def check():
        from google.protobuf import json_format
        from google.protobuf.struct_pb2 import Struct
        async def handler(request_bytes,context):
            m=Struct();m.ParseFromString(request_bytes)
            data=json_format.MessageToDict(m)
            response=Struct();response.update({"value":"hello:"+data["id"]})
            return response.SerializeToString(deterministic=True)
        server=grpc.aio.server()
        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler(
            "sample.Registry",{"Lookup":grpc.unary_unary_rpc_method_handler(
                handler,request_deserializer=lambda b:b,response_serializer=lambda b:b)}),))
        port=server.add_insecure_port("127.0.0.1:0")
        assert port > 0
        await server.start()
        channel=grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        try:
            op=ApiOperation(name="lookup",protocol="grpc",effect_class="read_only",method="UNARY",
                target="/sample.Registry/Lookup",source_hash="a"*64,
                input_schema_json=canonical_json({"type":"object","properties":{"id":{"type":"string"}},"required":["id"],"additionalProperties":False}),
                output_schema_json=canonical_json({"type":"object","properties":{"value":{"type":"string"}},"required":["value"],"additionalProperties":False}))
            args={"id":"x"}
            result=await ApprovedGrpcAdapter(channel=channel,
                installed_codecs={op.target:protobuf_struct_codec(op.source_hash)},
                channel_approval=lambda _:None).call(op,args,scope_for(op,args))
            assert result.status=="succeeded" and result.value=={"value":"hello:x"}
        finally:
            await channel.close();await server.stop(grace=0)
    asyncio.run(check())
