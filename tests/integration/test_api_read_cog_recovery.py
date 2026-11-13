"""Exercise durable HTTPS read settlement on PostgreSQL with a worker RPC double."""

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
from potbot.controller.execution_cogs import ExecutionCogRuntime
from potbot.controller.tasks import TaskService
from potbot.domain.errors import IndeterminateExecutionError, ScopeDeniedError
from potbot.domain.serialization import canonical_json
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus
from potbot.semantics.service import SynService
from potbot.tools.api_contracts import ApiOperation
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
from potbot.tools.rest_cog import install_graphql_cog, install_rest_cog
from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor
from tests.integration.test_repository_states import setup

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("graphql", [False, True], ids=["REST", "GraphQL"])
def test_api_read_cog_recovery_never_resends(app_env, tmp_path, monkeypatch, graphql):
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
            capabilities=("test",),
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

    registry = BoundCogRegistry.provenance(rest_read=not graphql, graphql_read=graphql)
    sealed = SynService(db, artifacts).import_snapshot(actor, repo, "api-read", registry.snapshot)
    origin = "https://api.fixture.example"
    request_group = "variables" if graphql else "path"
    operation = ApiOperation(
        "lookup",
        "graphql" if graphql else "rest",
        "read_only",
        "/graphql" if graphql else "/item/{id}",
        "POST" if graphql else "GET",
        canonical_json(
            {
                "type": "object",
                "required": [request_group],
                "additionalProperties": False,
                "properties": {
                    request_group: {
                        "type": "object",
                        "required": ["id"],
                        "additionalProperties": False,
                        "properties": {"id": {"type": "string"}},
                    }
                },
            }
        ),
        canonical_json(
            {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            }
        ),
        "a" * 64,
        graphql_document="query lookup($id: ID!) { value(id: $id) }" if graphql else None,
        timeout_ms=2000,
        max_output_bytes=1024,
    )
    config_hash = graphql_read_configuration_hash if graphql else rest_read_configuration_hash
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
    if graphql:
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
            return None

        def authorize_syn(self, *_):
            return None

        async def execute(self, op, _value, cog, _actor, _agent, *, syn_authorization, api_read):
            invocations.append(cog.invocation_id)
            response = await asyncio.to_thread(api_read)
            if self.lose:
                raise IndeterminateExecutionError("Fixture lost the reply after settlement")
            return TypedCogValue(op.binding.output_type_ref, response)

    agent = uuid4()

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
            **profile,
        )

    signature = WORK_COGS[WORK_KINDS.index("api")]
    source = TypedCogValue(
        signature.input_type_ref,
        {
            "schema_version": 1,
            "operation_ref": worker.binding.operation_ref,
            "input": {
                "type_ref": worker.binding.input_type_ref,
                "value": {
                    "arguments": {request_group: {"id": "test"}},
                },
            },
        },
    )
    good = CogRunContext(registry.snapshot.fingerprint, revision, state["id"], uuid4())
    lost = replace(good, invocation_id=uuid4())
    result = asyncio.run(controller(False).invoke(signature, source, good))
    assert result.value.value["result"]["value"]["value"] == {"value": 42}
    with pytest.raises(IndeterminateExecutionError, match="lost the reply"):
        asyncio.run(controller(True).invoke(signature, source, lost))
    fresh = controller(False)
    recover = fresh.recover_graphql_read if graphql else fresh.recover_rest_read
    receipt = recover(lost.invocation_id)
    assert receipt["receipt"]["value"] == {"value": 42}
    assert receipt["state"] == "receipt_recorded" and receipt["cog_stage"] == "indeterminate"
    assert receipt["usage"] == {"calls": 1, "tool_ms": 2000}
    assert recover(lost.invocation_id) == receipt
    wrong_profile = fresh.recover_rest_read if graphql else fresh.recover_graphql_read
    with pytest.raises(ScopeDeniedError):
        wrong_profile(lost.invocation_id)
    with pytest.raises(ScopeDeniedError):
        asyncio.run(fresh.invoke(signature, source, lost))
    assert requests == [operation.method, operation.method]
    assert invocations == [good.invocation_id, lost.invocation_id]
    balance = BudgetLedger(db).inspect(actor, episode)
    assert balance["used"] == {"calls": 2, "tool_ms": 4000} and balance["held"] == {}

    lose_provider_response = True
    unresolved = replace(good, invocation_id=uuid4())
    with pytest.raises(IndeterminateExecutionError, match="requires retained evidence"):
        asyncio.run(controller(False).invoke(signature, source, unresolved))
    assert recover(unresolved.invocation_id) == {
        "state": "unresolved",
        "cog_stage": "indeterminate",
        "budget_status": "indeterminate",
        "automatic_retry_permitted": False,
    }
    assert requests == [operation.method] * 3
    balance = BudgetLedger(db).inspect(actor, episode)
    assert balance["used"] == {"calls": 2, "tool_ms": 4000}
    assert balance["held"] == {"calls": 1, "tool_ms": 2000, "concurrency": 1}
