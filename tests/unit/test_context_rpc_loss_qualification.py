"""Probe the qualification fault boundaries without a managed host."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.context.rpc_contract import SCHEMA
from potbot.context.syn_context_route import METHOD, SynContextRoute, WorkerRpcBinding
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import IndeterminateExecutionError
from scripts.context_rpc_loss_qualification import drop_work_result_reply, verify_work_result_loss


@pytest.mark.parametrize("point", ["before_commit", "after_commit"])
def test_work_result_loss_tracks_one_original_request(point):
    actor = WorkspaceContext(uuid4(), uuid4())
    agent, run, request_id = uuid4(), uuid4(), uuid4()
    boot = "a" * 32
    checksum = "b" * 64
    row = {"state": "revoked", "result_request_id": None, "request_id": None, "result_sha256": None}

    def round_trip(transport_id, payload):
        header = {
            "profile": "syn.local-rpc-dispatch/1",
            "workspace_id": str(actor.workspace_id),
            "method": METHOD,
            "request_id": str(transport_id),
            "size": len(payload),
            "boot_id": boot,
            "caller_uid": 1000,
        }
        return route.accept(header, payload), boot

    def execute(request):
        row.update(
            result_request_id=request.request_id,
            request_id=request.request_id,
            result_sha256=checksum,
        )
        return {"revision": 1, "sha256": checksum}

    route = SynContextRoute(
        workspace_id=actor.workspace_id, operator_boot=boot, caller_uid=1000, round_trip=round_trip
    )
    binding = WorkerRpcBinding(actor.workspace_id, agent, run, "b" * 64, boot, "c" * 64, "lease", 1)
    invocation = route.bind(binding, lambda _: None, execute)
    payload = json.dumps(
        {
            "schema": SCHEMA,
            "method": "work.result",
            "request_id": str(request_id),
            "result": {"schema_version": 1, "type_ref": "syn://type/example", "value": "fixture"},
        }
    ).encode()
    try:
        with drop_work_result_reply(route, request_id, point) as loss:
            with pytest.raises(IndeterminateExecutionError, match="original request ID"):
                invocation.call(payload)
    finally:
        invocation.close()

    @contextmanager
    def transaction(selected_actor):
        assert selected_actor == actor

        def query(_sql, parameters):
            assert parameters == (actor.workspace_id, agent, run)
            return SimpleNamespace(fetchall=lambda: [row])

        yield SimpleNamespace(execute=query)

    evidence = verify_work_result_loss(
        SimpleNamespace(transaction=transaction),
        actor,
        agent_id=agent,
        run_id=run,
        observed=loss,
        result_sha256=checksum,
    )
    assert evidence["request_id"] == str(request_id)
    assert evidence["postgres_result_committed"] is (point == "after_commit")
    row["request_id"] = uuid4()
    with pytest.raises(AssertionError, match="original request"):
        verify_work_result_loss(
            SimpleNamespace(transaction=transaction),
            actor,
            agent_id=agent,
            run_id=run,
            observed=loss,
            result_sha256=checksum,
        )
