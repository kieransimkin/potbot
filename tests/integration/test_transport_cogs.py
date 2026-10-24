"""Real admitted HTTP/GraphQL/gRPC/MCP/CLI calls through monitored installed Cogs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID, uuid4

import grpc
import httpx
import pytest
from google.protobuf import json_format
from google.protobuf.struct_pb2 import Struct

from potbot import __version__
from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.cogs.execution import WORK_COGS
from potbot.cogs.registry import BoundCogRegistry
from potbot.controller.execution_cogs import ExecutionCogRuntime, HostOperation
from potbot.controller.execution_events import ExecutionEventService
from potbot.controller.transport_cogs import (
    TransportPorts,
    api_operation,
    cli_operation,
    mcp_operation,
)
from potbot.domain.errors import IndeterminateExecutionError, ScopeDeniedError
from potbot.domain.execution import ActionProposal, CapabilityContext, ToolDefinition, ToolRegistry
from potbot.domain.serialization import canonical_json
from potbot.semantics.service import SynService
from potbot.tools.api_contracts import ApiOperation
from potbot.tools.api_grpc import ApprovedGrpcAdapter, protobuf_struct_codec
from potbot.tools.api_http import ApprovedHttpApiAdapter
from potbot.tools.capability_contracts import ToolInstallationDescriptor
from potbot.tools.cli_adapter import ApprovedCliAdapter
from potbot.tools.execution_contracts import WORK_KINDS, ExecutionPolicy, WorkBinding
from potbot.tools.interactive_supervisor import InteractiveProcessSupervisor
from potbot.tools.mcp_sandbox import ReadOnlyMcpGrant, SandboxedMcpClient
from potbot.tools.semantic_contracts import QUERY, RESULTS
from potbot.tools.supervisor import ProcessSupervisor
from potbot.workspace_sandbox import WorkspaceSandbox
from tests.integration.test_repository_states import setup
from tests.unit.test_debugger_cogs_live import assert_lifecycles

pytestmark = [pytest.mark.postgres, pytest.mark.sandbox]
ROOT = Path(__file__).resolve().parents[2]


def test_monitored_installed_transport_cogs_real_services_and_confinement(app_env, tmp_path):
    fixture, artifacts, states, env, _, _ = setup(app_env, tmp_path)
    state = states.capture(
        app_env.context, app_env.repository_id, fixture.path, env, committed_ref="HEAD"
    )
    registry = BoundCogRegistry.provenance()
    sealed = SynService(app_env.db, artifacts).import_snapshot(
        app_env.context, app_env.repository_id, "transport-cogs-v1", registry.snapshot
    )
    policy = ExecutionPolicy("a" * 64, "b" * 64, state["content_digest"], __version__)
    source = TypedCogValue(QUERY, {"schema_version": 1, "text": "30,12", "limit": 1})
    requests, admissions, results = [], [], []

    def output(value):
        return {
            "schema_version": 1,
            "query_hash": source.fingerprint,
            "corpus_hash": "c" * 64,
            "items": [{"id": "sum", "text": str(value), "source_sha256": "d" * 64}],
            "complete": True,
        }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body))
            if self.path == "/uncertain":
                self.close_connection = True
                return
            value = body.get("variables", body)
            response = {"sum": value["a"] + value["b"]}
            raw = json.dumps({"data": response} if self.path == "/graphql" else response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    sandbox = WorkspaceSandbox.prepare(tmp_path / "transport-workspace")
    candidate = sandbox.path("candidates") / "admitted-tools"
    candidate.mkdir(mode=0o700)
    target = candidate / "fixture.py"
    shutil.copyfile(ROOT / "tests/fixtures/mcp_stdio_server.py", target)
    executable_hash = hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
    server_definition = ToolDefinition(
        name="fixture-mcp",
        capability="tool.read-local",
        argv_prefix=(sys.executable, "-u", str(target)),
        mutates=False,
        accepts_stdin=True,
        timeout_ms=60000,
        max_output_bytes=8192,
    )
    cli_definition = ToolDefinition(
        name="fixture-cli",
        capability="tool.read-local",
        argv_prefix=(sys.executable, "-c", "print(30 + 12)"),
        mutates=False,
        timeout_ms=5000,
        max_output_bytes=8192,
    )
    cancel_definition = ToolDefinition(
        name="fixture-cli-cancel",
        capability="tool.read-local",
        argv_prefix=(
            sys.executable,
            "-c",
            "import os,time; from pathlib import Path; "
            "Path(os.environ['TMPDIR'],'cog-cancel-started').write_text('started'); time.sleep(30)",
        ),
        mutates=False,
        timeout_ms=10000,
        max_output_bytes=8192,
    )
    observations = []

    class ObservedSupervisor(ProcessSupervisor):
        def run(self, request, *, cancelled=None, redactor=None):
            result = super().run(request, cancelled=cancelled, redactor=redactor)
            observations.append(result)
            return result

    cli = ApprovedCliAdapter(
        supervisor=ObservedSupervisor(),
        sandbox=sandbox,
        candidate_root=candidate,
        registry=ToolRegistry((cli_definition, cancel_definition)),
        capabilities=CapabilityContext((cli_definition.name, cancel_definition.name)),
        task_contract={"capabilities": (cli_definition.capability,)},
        pinned_executables={sys.executable: executable_hash},
    )

    def ports(encode, decode):
        return TransportPorts(QUERY, RESULTS, "e" * 64, encode, decode)

    def args(_):
        return {"a": 30, "b": 12}

    input_schema = {"type": "object"}
    output_schema = {
        "type": "object",
        "properties": {"sum": {"type": "number"}},
        "required": ["sum"],
    }
    installation = ToolInstallationDescriptor(
        "syn://thing/fixture-api-installation",
        app_env.context.workspace_id,
        "f" * 64,
        policy.configuration_hash,
        policy.policy_revision_hash,
        ("fixture.sum",),
    )

    def approve(*values):
        admissions.append("api")

    async def scenario():
        async def grpc_handler(payload, context):
            row = Struct()
            row.ParseFromString(payload)
            data = json_format.MessageToDict(row)
            result = Struct()
            result.update({"sum": data["a"] + data["b"]})
            requests.append(("grpc", data))
            return result.SerializeToString(deterministic=True)

        server = grpc.aio.server()
        server.add_generic_rpc_handlers(
            (
                grpc.method_handlers_generic_handler(
                    "fixture.Sum",
                    {
                        "Add": grpc.unary_unary_rpc_method_handler(
                            grpc_handler,
                            request_deserializer=lambda b: b,
                            response_serializer=lambda b: b,
                        )
                    },
                ),
            )
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        runtime = None
        try:
            async with (
                grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel,
                httpx.AsyncClient(trust_env=False) as client,
                SandboxedMcpClient(
                    supervisor=InteractiveProcessSupervisor(),
                    sandbox=sandbox,
                    candidate_root=candidate,
                    server_kind=server_definition.name,
                    registry=ToolRegistry((server_definition,)),
                    capabilities=CapabilityContext((server_definition.name,)),
                    task_contract={"capabilities": (server_definition.capability,)},
                    executable_sha256=executable_hash,
                    permitted=(ReadOnlyMcpGrant("add"),),
                    approve_call=lambda *_: None,
                ) as mcp,
            ):
                http_adapter = ApprovedHttpApiAdapter(
                    origin=f"http://127.0.0.1:{http.server_address[1]}",
                    client=client,
                    test_loopback=True,
                )
                operations = []
                for protocol in ("rest", "graphql", "grpc"):
                    op = ApiOperation(
                        name=f"sum-{protocol}",
                        protocol=protocol,
                        effect_class="read_only",
                        target="/fixture.Sum/Add" if protocol == "grpc" else f"/{protocol}",
                        method="UNARY" if protocol == "grpc" else "POST",
                        source_hash="a" * 64,
                        input_schema_json=canonical_json(input_schema),
                        output_schema_json=canonical_json(output_schema),
                        timeout_ms=5000,
                        max_output_bytes=8192,
                        graphql_document="query Add($a:Int!,$b:Int!){sum(a:$a,b:$b)}"
                        if protocol == "graphql"
                        else None,
                    )
                    encoder = (
                        (lambda _: {"body": args(None)})
                        if protocol == "rest"
                        else (lambda _: {"variables": args(None)})
                        if protocol == "graphql"
                        else args
                    )
                    operations.append(
                        api_operation(
                            operation_ref=f"syn://thing/fixture-{protocol}",
                            policy=policy,
                            ports=ports(encoder, lambda r: output(int(r["sum"]))),
                            adapter=ApprovedGrpcAdapter(
                                channel=channel,
                                installed_codecs={
                                    "/fixture.Sum/Add": protobuf_struct_codec("a" * 64)
                                },
                                channel_approval=lambda _: None,
                            )
                            if protocol == "grpc"
                            else http_adapter,
                            operation=op,
                            installation=installation,
                            authorize=approve,
                            egress=lambda _: None,
                        )
                    )
                operations.append(
                    mcp_operation(
                        operation_ref="syn://thing/fixture-mcp-add",
                        policy=policy,
                        ports=ports(args, lambda r: output(int(r["sum"]))),
                        client=mcp,
                        tool_name="add",
                        catalog_sha256=mcp.catalog.fingerprint,
                        tool_sha256=mcp.catalog.tool("add").fingerprint,
                        installation_sha256="a" * 64,
                        effect_class="read_only",
                        authorize=lambda *_: None,
                    )
                )
                operations.append(
                    cli_operation(
                        operation_ref="syn://thing/fixture-cli-sum",
                        policy=policy,
                        ports=ports(
                            lambda _: ActionProposal(kind=cli_definition.name).to_dict(),
                            lambda r: output(r.stdout.strip()),
                        ),
                        adapter=cli,
                        tool_name=cli_definition.name,
                    )
                )
                cancelled = cli_operation(
                    operation_ref="syn://thing/fixture-cli-cancel",
                    policy=policy,
                    ports=ports(
                        lambda _: ActionProposal(kind=cancel_definition.name).to_dict(),
                        lambda r: output(r.stdout.strip()),
                    ),
                    adapter=cli,
                    tool_name=cancel_definition.name,
                )
                write = ApiOperation(
                    name="uncertain-write",
                    protocol="rest",
                    effect_class="non_idempotent_write",
                    target="/uncertain",
                    method="POST",
                    source_hash="a" * 64,
                    input_schema_json=canonical_json(input_schema),
                    output_schema_json=canonical_json(output_schema),
                    timeout_ms=5000,
                    max_output_bytes=8192,
                )
                uncertain = api_operation(
                    operation_ref="syn://thing/fixture-write",
                    policy=policy,
                    ports=ports(lambda _: {"body": args(None)}, lambda r: output(r["sum"])),
                    adapter=http_adapter,
                    operation=write,
                    installation=installation,
                    authorize=approve,
                    egress=lambda _: None,
                )

                async def workflow(call, value):
                    for installed in operations:
                        result = await call.child(
                            installed.binding.kind, installed.binding.operation_ref, value
                        )
                        assert result.value.value["result"]["value"]["items"][0]["text"] == "42"
                    return TypedCogValue(RESULTS, output("five admitted transports"))

                root = HostOperation(
                    WorkBinding(
                        "workflow",
                        "syn://thing/fixture-transport-workflow",
                        QUERY,
                        RESULTS,
                        "a" * 64,
                        "read_only",
                        policy,
                    ),
                    workflow,
                )
                runtime = ExecutionCogRuntime(
                    database=app_env.db,
                    artifacts=artifacts,
                    actor=app_env.context,
                    snapshot_id=UUID(sealed["scope"]["snapshot_id"]),
                    task_revision_id=uuid4(),
                    repository_state_id=state["id"],
                    emitter_run_id=uuid4(),
                    policy=policy,
                    operations=(*operations, cancelled, uncertain, root),
                    admit=lambda *_: None,
                )

                async def invoke(operation):
                    signature = WORK_COGS[WORK_KINDS.index(operation.binding.kind)]
                    return await registry.invoke(
                        str(signature.cog_ref),
                        TypedCogValue(
                            signature.input_type_ref,
                            {
                                "schema_version": 1,
                                "operation_ref": operation.binding.operation_ref,
                                "input": {"type_ref": QUERY, "value": source.value},
                            },
                        ),
                        CogRunContext(
                            registry.snapshot.fingerprint,
                            runtime.task_revision,
                            state["id"],
                            uuid4(),
                        ),
                        controller=runtime,
                    )

                results.append(await invoke(root))
                cancellation = asyncio.create_task(invoke(cancelled))
                try:
                    async with asyncio.timeout(7):
                        while not (sandbox.path("tmp") / "cog-cancel-started").exists():
                            if cancellation.done():
                                await cancellation
                                pytest.fail("Cancellation fixture exited before signalling start")
                            await asyncio.sleep(0.01)
                    cancellation.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await cancellation
                    assert observations[-1].outcome == "cancelled"
                    assert observations[-1].processes_stopped
                finally:
                    if not cancellation.done():
                        cancellation.cancel()
                        try:
                            await cancellation
                        except asyncio.CancelledError:
                            pass
                with pytest.raises(IndeterminateExecutionError):
                    await invoke(uncertain)
                with pytest.raises(ScopeDeniedError):
                    await invoke(root)
                assert len([r for r in requests if r[0] == "/uncertain"]) == 1
                assert mcp.isolation_identity
        finally:
            await server.stop(grace=0)
        return runtime

    try:
        runtime = asyncio.run(scenario())
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)
    assert runtime is not None and not thread.is_alive()
    events = ExecutionEventService(app_env.db).events(app_env.context, limit=256)["items"]
    assert_lifecycles(events, results)
    completed = [e for e in events if e["stage"] == "completed"]
    assert len(completed) == 6  # parent plus five actual transport calls
    assert len([e for e in events if e["stage"] == "indeterminate"]) == 1
    assert len([e for e in events if e["stage"] == "cancelled"]) == 1
    assert len({e["run_id"] for e in completed}) == 6
    assert len(admissions) >= 4
    evidence_dir = os.environ.get("POTBOT_TRANSPORT_EVIDENCE_DIR")
    if evidence_dir:
        path = Path(evidence_dir)
        path.mkdir(parents=True, exist_ok=True)
        sources = (
            "src/potbot/controller/transport_cogs.py",
            "src/potbot/controller/execution_cogs.py",
            "src/potbot/tools/cli_adapter.py",
            "tests/integration/test_transport_cogs.py",
        )
        import subprocess

        report = {
            "result": "passed",
            "version": __version__,
            "source_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "implementation_source": {
                p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in sources
            },
            "transports": ["rest", "graphql", "grpc", "sandboxed_mcp_stdio", "sandboxed_cli"],
            "events": events,
            "completed_runs": len(completed),
            "uncertain_write_attempts": 1,
            "cancelled_process_tree_stopped": observations[-1].processes_stopped,
            "qualification": "local_services_and_linux_sandbox_not_live_provider_or_device",
        }
        (path / "transport-cogs.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
