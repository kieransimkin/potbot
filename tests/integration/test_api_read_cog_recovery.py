"""Exercise durable API read settlement on PostgreSQL with a worker RPC double."""

import asyncio
import json
import socket
from dataclasses import replace
from uuid import UUID, uuid4

import httpx
import pytest

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.cogs.execution import WORK_COGS
from potbot.cogs.registry import BoundCogRegistry
from potbot.controller.budgets import BudgetLedger
from potbot.controller.cog_capability_policy import (
    Capability,
    CogCapabilityGrant,
    CogCapabilityPolicy,
    CogPolicyDenied,
    Denial,
    installation_hash,
)
from potbot.controller.cog_grant_store import publish_cog_policy
from potbot.controller.execution_cogs import ExecutionCogRuntime
from potbot.controller.tasks import TaskService
from potbot.domain.errors import IndeterminateExecutionError, ScopeDeniedError
from potbot.domain.serialization import canonical_json
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus
from potbot.semantics.service import SynService
from potbot.tools.api_contracts import ApiOperation
from potbot.tools.api_grpc import InstalledGrpcRead, grpc_read_configuration_hash
from potbot.tools.api_https import (
    InstalledGraphqlRead,
    InstalledRestRead,
    graphql_read_configuration_hash,
    rest_read_configuration_hash,
)
from potbot.tools.capability_contracts import (
    ToolImplementationDescriptor,
    ToolInstallationDescriptor,
)
from potbot.tools.execution_contracts import WORK_KINDS, ExecutionPolicy
from potbot.tools.rest_cog import install_graphql_cog, install_grpc_cog, install_rest_cog
from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor
from tests.integration.test_repository_states import setup
from tests.integration.test_tool_phase2_4_local_services import pinned_grpc_provider  # noqa: F401

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("protocol", ["rest", "graphql", "grpc", "grpc-persistent"])
def test_api_read_cog_recovery_never_resends(app_env, tmp_path, monkeypatch, request, protocol):
    persistent = protocol == "grpc-persistent"
    if persistent:
        protocol = "grpc"
    graphql = protocol == "graphql"
    grpc = protocol == "grpc"
    _, artifacts, _, _, state, _ = setup(app_env, tmp_path)
    actor, db, repo = app_env.context, app_env.db, app_env.repository_id
    tasks = TaskService(db)
    task = tasks.submit(
        actor,
        repo,
        ContractSpec(
            objective="Recover a provider result after a worker reply is lost",
            baseline_ref="git:" + "a" * 40,
            deliverable_type=DeliverableType.INVESTIGATION,
            requirements=(Requirement("R1", "Recover result", "No repeated request"),),
            change_scope=(),
            capabilities=("read", "network", "external_disclosure"),
            verification_policy="checks-v1",
            budget=(("tokens", 0), ("calls", 3), ("tool_ms", 6000), ("concurrency", 1)),
        ),
        "api-read",
    )
    episode = UUID(task["task_id"])
    revision = UUID(task["current_contract"]["id"])
    tasks.transition(
        actor, episode, 0, TaskStatus.INITIALIZING, "Initialize", idempotency_key="init"
    )
    tasks.transition(actor, episode, 1, TaskStatus.RUNNING, "Run", idempotency_key="run")

    registry = BoundCogRegistry.provenance(
        rest_read=protocol == "rest",
        graphql_read=graphql,
        grpc_read=grpc,
    )
    sealed = SynService(db, artifacts).import_snapshot(actor, repo, "api-read", registry.snapshot)
    origin = "https://api.fixture.example"
    request_group = "variables" if graphql else "path"
    input_schema = {
        "type": "object",
        "required": ["id"],
        "additionalProperties": False,
        "properties": {"id": {"type": "string"}},
    }
    target, method = {
        "rest": ("/item/{id}", "GET"),
        "graphql": ("/graphql", "POST"),
        "grpc": ("/sample.Registry/Lookup", "UNARY"),
    }[protocol]
    operation = ApiOperation(
        "lookup",
        protocol,
        "read_only",
        target,
        method,
        canonical_json(
            input_schema
            if grpc
            else {
                "type": "object",
                "required": [request_group],
                "additionalProperties": False,
                "properties": {request_group: input_schema},
            }
        ),
        canonical_json(
            {
                "type": "object",
                "properties": {"value": {"type": "string" if grpc else "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            }
        ),
        "a" * 64,
        graphql_document="query lookup($id: ID!) { value(id: $id) }" if graphql else None,
        timeout_ms=2000,
        max_output_bytes=1024,
    )
    config_hash = {
        "rest": rest_read_configuration_hash,
        "graphql": graphql_read_configuration_hash,
        "grpc": grpc_read_configuration_hash,
    }[protocol]
    if grpc:
        origin, codec, provider_requests, _, _ = request.getfixturevalue("pinned_grpc_provider")
    implementation = ToolImplementationDescriptor(
        "syn://thing/fixture-api-read",
        operation.to_capability().fingerprint,
        "a" * 64,
        "Fixture",
        "1",
    )
    installation = ToolInstallationDescriptor(
        "syn://thing/fixture-api-installation",
        actor.workspace_id,
        implementation.fingerprint,
        config_hash(actor, origin, operation),
        "b" * 64,
        ("read",),
    )
    policy = ExecutionPolicy(
        installation.policy_revision_hash,
        installation.configuration_hash,
        state["content_digest"],
        "fixture",
    )
    if grpc:
        installed = InstalledGrpcRead(actor, origin, operation, codec, implementation, installation)
        worker = install_grpc_cog(installed, policy, "d" * 64, "e" * 64)
        profile = {"grpc_read": installed}
    elif graphql:
        installed = InstalledGraphqlRead(actor, origin, operation, implementation, installation)
        worker = install_graphql_cog(installed, policy, "d" * 64, "e" * 64)
        profile = {"graphql_read": installed}
    else:
        installed = InstalledRestRead(actor, origin, operation, implementation, installation)
        worker = install_rest_cog(installed, policy, "d" * 64, "e" * 64)
        profile = {"rest_read": installed}

    requests = []
    invocations = []
    lose_provider_response = False
    expire_before_provider = False
    revoke_before_provider = False
    clock = [1000]
    monkeypatch.setattr("potbot.controller.cog_capability_policy.time", lambda: clock[0])

    def resolve(host, port, **_):
        assert host == "api.fixture.example" and port == 443
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.215.14", port))
        ]

    async def provider(_, request):
        requests.append(request.method)
        assert request.url.host == "93.184.215.14"
        if graphql:
            assert request.url.path == "/graphql"
            assert json.loads(await request.aread())["variables"] == {"id": "test"}
        else:
            assert request.url.path == "/item/test"
        if lose_provider_response:
            raise httpx.ReadTimeout("Fixture lost the provider response")
        return httpx.Response(200, json={"data": {"value": 42}} if graphql else {"value": 42})

    if not grpc:
        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", provider)

    class ReplyLossWorker(SandboxedCogExecutor):
        """Only the signed RPC acknowledgement is simulated."""

        def __init__(self, lose):
            self.lose = lose

        def bind_context(self, *_):
            pass

        def syn_probe(self, *_):
            pass

        def syn_provenance(self, *_):
            return {
                "profile": "potbot.syn-cog-native-binding/1",
                "workspace_id": str(actor.workspace_id),
                "agent_id": str(agent),
                "node": "f" * 64,
            }

        def authorize_syn(self, *_):
            return None

        async def execute(self, op, _value, cog, _actor, _agent, *, syn_authorization, api_read):
            invocations.append(cog.invocation_id)
            if expire_before_provider:
                clock[0] = 1200
            if revoke_before_provider:
                publish_cog_policy(
                    app_env.admin_dsn, app_env.schema, replace(capability_policy, grants=()), 1
                )
            response = await asyncio.to_thread(api_read)
            if self.lose:
                raise IndeterminateExecutionError("Fixture lost the reply after settlement")
            return TypedCogValue(op.binding.output_type_ref, response)

    agent = uuid4()
    capability_policy = CogCapabilityPolicy(
        actor,
        agent,
        "f" * 64,
        policy.policy_revision_hash,
        (
            CogCapabilityGrant(
                installation_hash(worker),
                frozenset({Capability.READ, Capability.NETWORK, Capability.EXTERNAL_DISCLOSURE}),
                1200,
            ),
        ),
    )
    if persistent:
        publish_cog_policy(app_env.admin_dsn, app_env.schema, capability_policy, 0)

    def controller(lose):
        return ExecutionCogRuntime(
            database=db,
            artifacts=artifacts,
            actor=actor,
            snapshot_id=UUID(sealed["scope"]["snapshot_id"]),
            task_revision_id=revision,
            repository_state_id=state["id"],
            emitter_run_id=uuid4(),
            policy=policy,
            operations=(worker,),
            admit=lambda *_: None,
            sandbox_executor=ReplyLossWorker(lose),
            sandbox_agent_id=agent,
            capability_policy=None if persistent else capability_policy,
            persistent_cog_grants=persistent,
            **profile,
        )

    signature = WORK_COGS[WORK_KINDS.index("api")]

    def input_for(identifier):
        arguments = {"id": identifier}
        return TypedCogValue(
            signature.input_type_ref,
            {
                "schema_version": 1,
                "operation_ref": worker.binding.operation_ref,
                "input": {
                    "type_ref": worker.binding.input_type_ref,
                    "value": {"arguments": arguments if grpc else {request_group: arguments}},
                },
            },
        )

    source = input_for("test")
    expected = {"value": "hello:test"} if grpc else {"value": 42}
    good = CogRunContext(registry.snapshot.fingerprint, revision, state["id"], uuid4())
    lost = replace(good, invocation_id=uuid4())
    result = asyncio.run(controller(False).invoke(signature, source, good))
    assert result.value.value["result"]["value"]["value"] == expected
    with pytest.raises(IndeterminateExecutionError, match="lost the reply"):
        asyncio.run(controller(True).invoke(signature, source, lost))
    fresh = controller(False)
    recover = getattr(fresh, f"recover_{protocol}_read")
    receipt = recover(lost.invocation_id)
    assert receipt["receipt"]["value"] == expected
    assert receipt["state"] == "receipt_recorded" and receipt["cog_stage"] == "indeterminate"
    assert receipt["usage"] == {"calls": 1, "tool_ms": 2000}
    assert recover(lost.invocation_id) == receipt
    wrong_profile = fresh.recover_rest_read if graphql else fresh.recover_graphql_read
    with pytest.raises(ScopeDeniedError):
        wrong_profile(lost.invocation_id)
    with pytest.raises(ScopeDeniedError):
        asyncio.run(fresh.invoke(signature, source, lost))
    if grpc:
        assert provider_requests == [{"id": "test"}] * 2
    else:
        assert requests == [operation.method, operation.method]
    assert invocations == [good.invocation_id, lost.invocation_id]
    balance = BudgetLedger(db).inspect(actor, episode)
    assert balance["used"] == {"calls": 2, "tool_ms": 4000} and balance["held"] == {}

    expire_before_provider = not persistent
    revoke_before_provider = persistent
    denied = replace(good, invocation_id=uuid4())
    denial = Denial.REPLACED if persistent else Denial.EXPIRED
    with pytest.raises(CogPolicyDenied, match=denial.value):
        asyncio.run(controller(False).invoke(signature, source, denied))
    assert recover(denied.invocation_id) == {
        "state": "denied_before_provider",
        "cog_stage": "failed",
        "budget_status": "released",
        "automatic_retry_permitted": False,
    }
    if grpc:
        assert provider_requests == [{"id": "test"}] * 2
    else:
        assert requests == [operation.method] * 2
    assert BudgetLedger(db).inspect(actor, episode) == balance
    expire_before_provider = False
    revoke_before_provider = False
    if persistent:
        with pytest.raises(CogPolicyDenied, match=Denial.INSTALLATION.value):
            asyncio.run(
                controller(False).invoke(signature, source, replace(good, invocation_id=uuid4()))
            )
        assert invocations == [good.invocation_id, lost.invocation_id, denied.invocation_id]
        publish_cog_policy(app_env.admin_dsn, app_env.schema, capability_policy, 2)
    clock[0] = 1000
    lose_provider_response = True
    unresolved = replace(good, invocation_id=uuid4())
    with pytest.raises(IndeterminateExecutionError, match="requires retained evidence"):
        asyncio.run(controller(False).invoke(signature, input_for("provider-lost"), unresolved))
    assert recover(unresolved.invocation_id) == {
        "state": "unresolved",
        "cog_stage": "indeterminate",
        "budget_status": "indeterminate",
        "automatic_retry_permitted": False,
    }
    if grpc:
        assert provider_requests == [{"id": "test"}, {"id": "test"}, {"id": "provider-lost"}]
    else:
        assert requests == [operation.method] * 3
    balance = BudgetLedger(db).inspect(actor, episode)
    assert balance["used"] == {"calls": 2, "tool_ms": 4000}
    assert balance["held"] == {"calls": 1, "tool_ms": 2000, "concurrency": 1}
