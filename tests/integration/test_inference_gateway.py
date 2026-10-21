"""Real SQL admission, separate repairs, replay, cancellation and accounting."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest

from potbot.controller.budgets import BudgetLedger
from potbot.controller.execution import ExecutionStore
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.domain.execution import ToolRegistry
from potbot.domain.model_proposals import ModelCodingResponse
from potbot.domain.serialization import canonical_json
from potbot.domain.tasks import ContractSpec
from potbot.models.context import ContextPacketService, FixtureContextRenderer
from potbot.models.envelopes import RepairPolicy
from potbot.models.gateway import InferenceGateway
from potbot.models.invocations import GenerateRequest
from potbot.models.registry import DeploymentSpec, ModelRegistry
from potbot.models.transport import ScriptedTransport, Terminal, TextDelta, TokenUsage

from .test_coding_pipeline import setup

pytestmark = pytest.mark.postgres


def prepare(app_env, tmp_path, *, calls=20, concurrency=4, tokens=200000):
    """Extend the existing real repository fixture through public task APIs."""
    rig = setup(app_env, tmp_path)
    current = rig.tasks.inspect(app_env.context, rig.episode_id)
    contract = ContractSpec.from_dict(current["current_contract"]["content"])
    limits = {
        **dict(contract.budget),
        "calls": calls,
        "concurrency": concurrency,
        "tokens": tokens,
        "inference_ms": 300000,
        "money_microunits": 1000,
    }
    revised = rig.tasks.revise(
        app_env.context,
        rig.episode_id,
        rig.contract_id,
        replace(contract, budget=tuple(limits.items())),
        "inference-budget",
    )
    rig.contract_id = UUID(revised["current_contract"]["id"])
    rig.handoff = rig.coding.create_contract(
        app_env.context, rig.episode_id, rig.contract_id, rig.state["id"], rig.spec, "model-handoff"
    )
    inputs = ExecutionStore(app_env.db, ToolRegistry(())).create_inputs(
        app_env.context, rig.episode_id, rig.contract_id, rig.state["id"], "model-inputs"
    )
    rig.node_id = UUID(inputs["node_id"])
    rig.renderer = FixtureContextRenderer(canonical_model="potbot/inference-fixture")
    rig.deployment = DeploymentSpec(
        rig.renderer.canonical_model,
        "a" * 64,
        "potbot-offline-fixture",
        "1",
        "none",
        rig.renderer.tokenizer_revision,
        rig.renderer.chat_template_digest,
        "json-envelope-v1",
        "none",
        100000,
        4096,
        (("coding", rig.renderer.prompt_version),),
        ("coding",),
        ("coding-result-v1",),
    )
    deployed = ModelRegistry(app_env.db).register(app_env.context, rig.deployment)
    rig.contexts = ContextPacketService(app_env.db, rig.artifacts)
    rig.packet_arguments = dict(
        deployment_id=deployed["id"],
        spec=rig.deployment,
        renderer=rig.renderer,
        node_id=rig.node_id,
        response_schema="coding-result-v1",
        role="coding",
        output_limit=2048,
        implementation_id=UUID(rig.handoff["id"]),
    )
    rig.packet = rig.contexts.create(
        app_env.context, **rig.packet_arguments, idempotency_key="model-packet"
    )
    rig.request = GenerateRequest(
        deployed["id"],
        "coding",
        UUID(rig.packet["id"]),
        "coding-result-v1",
        '{"temperature":0}',
        2048,
        datetime.now(UTC) + timedelta(minutes=5),
        uuid4(),
    )
    rig.response = ModelCodingResponse(
        UUID(rig.handoff["id"]),
        rig.state["id"],
        rig.contract_id,
        patch=rig.patch,
        changed_files=("calculator.py",),
        proposed_checks=("calculator-tests",),
    )
    rig.raw = canonical_json(rig.response.to_dict()).encode()
    return rig


def events(rig, raw=None, *, usage=True):
    data = rig.raw if raw is None else raw
    return (
        TextDelta(data),
        Terminal(
            usage=TokenUsage(rig.packet["token_count"], math.ceil(len(data) / 4)) if usage else None
        ),
    )


def gateway(rig, responses=None):
    transport = ScriptedTransport(responses or (events(rig),))
    return InferenceGateway(rig.env.db, rig.artifacts, transport), transport


def test_fragmented_response_is_retained_once_with_exact_input_and_settled_usage(app_env, tmp_path):
    rig = prepare(app_env, tmp_path)
    chunks = tuple(TextDelta(rig.raw[i : i + 11]) for i in range(0, len(rig.raw), 11))
    instance, transport = gateway(rig, ((*chunks, events(rig)[1]),))
    result = instance.generate(app_env.context, rig.node_id, rig.request, "one", inference_ms=30000)
    assert result["status"] == "accepted" and result["usable"] is True
    assert result["executable"] is False and result["semantic_validation"] == "required"
    assert result["accounting"] == "settled" and result["fixture_only"] is True
    assert rig.artifacts.read(app_env.context, UUID(result["raw_output_artifact_id"])) == rig.raw
    assert transport.requests[0][1] == rig.packet["manifest"]["wire_text"]
    assert transport.requests[0][1] != canonical_json(rig.packet["manifest"])
    restarted = InferenceGateway(app_env.db, rig.artifacts, transport)
    assert (
        restarted.generate(app_env.context, rig.node_id, rig.request, "one", inference_ms=30000)
        == result
    )
    assert len(transport.requests) == 1
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM model_invocations").fetchone()["n"] == 1
    budget = BudgetLedger(app_env.db).inspect(app_env.context, rig.episode_id)
    assert budget["used"]["calls"] == 1 and budget["held"] == {}
    assert budget["used"]["tokens"] == rig.packet["token_count"] + math.ceil(len(rig.raw) / 4)


def test_two_workers_competing_for_last_call_cannot_both_dispatch(app_env, tmp_path):
    rig = prepare(app_env, tmp_path, calls=1, concurrency=1)
    instance, transport = gateway(rig, (events(rig), events(rig)))
    barrier = Barrier(2)

    def run(key):
        barrier.wait(timeout=10)
        return instance.generate(app_env.context, rig.node_id, rig.request, key, inference_ms=30000)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ("worker-a", "worker-b")))
    assert {item["status"] for item in results} == {"accepted", "budget_unavailable"}
    assert len(transport.requests) == 1
    denied = next(item for item in results if item["status"] == "budget_unavailable")
    assert denied["invocation_id"] is None and denied["accounting"] == "not_reserved"


def test_reusing_key_with_different_amounts_or_request_is_rejected(app_env, tmp_path):
    rig = prepare(app_env, tmp_path)
    instance, transport = gateway(rig)
    instance.prepare(app_env.context, rig.node_id, rig.request, "same", inference_ms=30000)
    with pytest.raises(RevisionConflictError):
        instance.prepare(app_env.context, rig.node_id, rig.request, "same", inference_ms=30001)
    with pytest.raises(RevisionConflictError):
        instance.prepare(
            app_env.context,
            rig.node_id,
            replace(rig.request, sampling_json='{"temperature":1}'),
            "same",
            inference_ms=30000,
        )
    assert transport.requests == []


def test_queue_cancellation_releases_capacity_without_invocation(app_env, tmp_path):
    rig = prepare(app_env, tmp_path)
    instance, transport = gateway(rig)
    instance.prepare(app_env.context, rig.node_id, rig.request, "cancel", inference_ms=30000)
    result = instance.cancel_queued(app_env.context, "cancel")
    assert result["status"] == "cancelled" and result["accounting"] == "released"
    assert instance.dispatch(app_env.context, "cancel") == result
    assert transport.requests == []
    budget = BudgetLedger(app_env.db).inspect(app_env.context, rig.episode_id)
    assert budget["held"] == {} and budget["used"] == {}


def test_revised_context_and_corrupt_input_release_before_dispatch(app_env, tmp_path):
    rig = prepare(app_env, tmp_path)
    instance, transport = gateway(rig)
    instance.prepare(app_env.context, rig.node_id, rig.request, "stale", inference_ms=30000)
    current = rig.tasks.inspect(app_env.context, rig.episode_id)
    spec = ContractSpec.from_dict(current["current_contract"]["content"])
    rig.tasks.revise(
        app_env.context,
        rig.episode_id,
        rig.contract_id,
        replace(spec, objective="A new objective"),
        "changed",
    )
    result = instance.dispatch(app_env.context, "stale")
    assert result["status"] == "stale_context" and result["accounting"] == "released"
    assert transport.requests == []


def test_corrupt_retained_source_is_not_dispatched_and_does_not_leak_reservation(app_env, tmp_path):
    rig = prepare(app_env, tmp_path)
    instance, transport = gateway(rig)
    instance.prepare(app_env.context, rig.node_id, rig.request, "corrupt", inference_ms=30000)
    source_id = UUID(rig.packet["manifest"]["sources"][0]["id"])
    metadata = rig.artifacts.metadata(app_env.context, source_id)
    (app_env.artifact_root / metadata["locator"]).write_bytes(b"corrupt retained bytes")
    result = instance.dispatch(app_env.context, "corrupt")
    assert result["status"] == "invalid_context" and result["accounting"] == "released"
    assert transport.requests == []


def test_bounded_repair_uses_fresh_context_and_retains_separate_outputs(app_env, tmp_path):
    rig = prepare(app_env, tmp_path)
    bad = b"This is prose, not the required JSON envelope."
    instance, transport = gateway(rig, (events(rig, bad), events(rig)))

    def repair(prior, ordinal):
        packet = rig.contexts.create(
            app_env.context,
            **rig.packet_arguments,
            idempotency_key=f"repair-packet-{ordinal}",
            repair_of=UUID(prior["invocation_id"]),
        )
        transport.responses = (
            transport.responses[0],
            (
                TextDelta(rig.raw),
                Terminal(usage=TokenUsage(packet["token_count"], math.ceil(len(rig.raw) / 4))),
            ),
        )
        return replace(rig.request, context_packet_id=UUID(packet["id"]))

    result = instance.generate_with_repair(
        app_env.context,
        rig.node_id,
        rig.request,
        "repair",
        inference_ms=30000,
        repair_request=repair,
    )
    assert result["status"] == "accepted" and result["usable"] is True
    assert [item["status"] for item in result["attempts"]] == ["schema_violation", "accepted"]
    assert len(transport.requests) == 2
    assert bad.decode() in transport.requests[1][1]
    assert len({item["invocation_id"] for item in result["attempts"]}) == 2
    assert (
        rig.artifacts.read(app_env.context, UUID(result["attempts"][0]["raw_output_artifact_id"]))
        == bad
    )
    budget = BudgetLedger(app_env.db).inspect(app_env.context, rig.episode_id)
    assert budget["used"]["calls"] == 2 and budget["held"] == {}


def test_repair_exhaustion_is_terminal_and_incomplete_transport_is_not_repaired(app_env, tmp_path):
    rig = prepare(app_env, tmp_path)
    instance, transport = gateway(rig, (events(rig, b"{bad"),))
    result = instance.generate_with_repair(
        app_env.context,
        rig.node_id,
        rig.request,
        "no-repair",
        inference_ms=30000,
        repair_request=lambda result, index: pytest.fail("Repair allowance was exhausted"),
        policy=RepairPolicy(max_repairs=0),
    )
    assert result["status"] == "schema_violation" and result["usable"] is False
    assert len(transport.requests) == 1
    incomplete, second = gateway(rig, ((TextDelta(rig.raw),),))
    result = incomplete.generate_with_repair(
        app_env.context,
        rig.node_id,
        rig.request,
        "partial",
        inference_ms=30000,
        repair_request=lambda result, index: pytest.fail("Uncertain transport must not be retried"),
    )
    assert result["status"] == "incomplete_stream" and result["usable"] is False
    assert result["attempts"][0]["accounting"] == "indeterminate" and len(second.requests) == 1


def test_lost_terminal_usage_is_conservative_and_reconciliation_does_not_accept_output(
    app_env, tmp_path
):
    rig = prepare(app_env, tmp_path)
    instance, transport = gateway(rig, (events(rig, usage=False),))
    result = instance.generate(
        app_env.context, rig.node_id, rig.request, "missing-usage", inference_ms=30000
    )
    assert result["status"] == "usage_unresolved" and result["accounting"] == "indeterminate"
    assert result["usable"] is False
    instance.reconcile(
        app_env.context, "missing-usage", TokenUsage(rig.packet["token_count"], 10), elapsed_ms=5
    )
    assert instance.inspect(app_env.context, "missing-usage")["result"]["accounting"] == "settled"
    replay = instance.generate(
        app_env.context, rig.node_id, rig.request, "missing-usage", inference_ms=30000
    )
    assert replay["usable"] is False and replay["accounting"] == "settled"
    assert len(transport.requests) == 1


def test_process_loss_after_dispatch_marker_never_repeats_backend_call(app_env, tmp_path):
    rig = prepare(app_env, tmp_path)

    class SimulatedProcessLoss(BaseException):
        pass

    class CrashTransport(ScriptedTransport):
        def stream(self, request, wire_text, operation_id):
            self.requests.append((request, wire_text, operation_id))
            raise SimulatedProcessLoss()
            yield  # pragma: no cover -- keeps the fixture a generator

    transport = CrashTransport((events(rig),))
    instance = InferenceGateway(app_env.db, rig.artifacts, transport)
    with pytest.raises(SimulatedProcessLoss):
        instance.generate(app_env.context, rig.node_id, rig.request, "crashed", inference_ms=30000)
    restarted = InferenceGateway(app_env.db, rig.artifacts, transport)
    result = restarted.generate(
        app_env.context, rig.node_id, rig.request, "crashed", inference_ms=30000
    )
    assert result["status"] == "indeterminate" and result["accounting"] == "indeterminate"
    assert result["usable"] is False and len(transport.requests) == 1


def test_usage_overrun_is_retained_and_output_limit_cannot_be_bypassed(app_env, tmp_path):
    rig = prepare(app_env, tmp_path)
    instance, _ = gateway(
        rig, ((TextDelta(rig.raw), Terminal(usage=TokenUsage(rig.packet["token_count"], 300000))),)
    )
    result = instance.generate(
        app_env.context, rig.node_id, rig.request, "overrun", inference_ms=30000
    )
    assert result["status"] == "output_limit" and result["usable"] is False
    budget = BudgetLedger(app_env.db).inspect(app_env.context, rig.episode_id)
    assert budget["used"]["tokens"] > budget["limits"]["tokens"]
    assert budget["available"]["tokens"] == 0
    assert rig.tasks.inspect(app_env.context, rig.episode_id)["lifecycle"] == "budget_exhausted"


def test_cancelled_terminal_retains_classification_and_fake_provider_usage_is_rejected(
    app_env, tmp_path
):
    rig = prepare(app_env, tmp_path)
    instance, _ = gateway(
        rig,
        ((TextDelta(b"partial"), Terminal("cancelled", TokenUsage(rig.packet["token_count"], 2))),),
    )
    result = instance.generate(
        app_env.context, rig.node_id, rig.request, "backend-cancelled", inference_ms=30000
    )
    assert result["status"] == "cancelled" and result["accounting"] == "settled"
    spoofed, _ = gateway(
        rig, ((TextDelta(rig.raw), Terminal(usage=TokenUsage(1, 1, origin="provider_reported"))),)
    )
    result = spoofed.generate(
        app_env.context, rig.node_id, rig.request, "spoofed", inference_ms=30000
    )
    assert result["status"] == "transport_failure" and result["accounting"] == "indeterminate"


def test_other_workspace_cannot_replay_or_inspect_operation(app_env, tmp_path):
    rig = prepare(app_env, tmp_path)
    instance, _ = gateway(rig)
    instance.prepare(app_env.context, rig.node_id, rig.request, "private", inference_ms=30000)
    other = InferenceGateway(app_env.other_db, rig.artifacts, ScriptedTransport((events(rig),)))
    with pytest.raises(ScopeDeniedError):
        other.inspect(app_env.other_context, "private")
    with pytest.raises(ScopeDeniedError):
        other.dispatch(app_env.other_context, "private")


def test_fixture_input_usage_cannot_silently_undercount_the_rendered_packet(app_env, tmp_path):
    rig = prepare(app_env, tmp_path)
    instance, _ = gateway(rig, ((TextDelta(rig.raw), Terminal(usage=TokenUsage(0, 10))),))
    result = instance.generate(
        app_env.context, rig.node_id, rig.request, "undercounted", inference_ms=30000
    )
    assert result["status"] == "usage_unresolved" and result["accounting"] == "indeterminate"
    assert result["input_usage_mismatch"] is True and result["usable"] is False
    assert result["reported_usage"]["input_tokens"] == 0
    assert (
        BudgetLedger(app_env.db).inspect(app_env.context, rig.episode_id)["held"]["tokens"]
        >= rig.packet["token_count"]
    )


def test_changed_repair_allowance_cannot_extend_an_existing_operation_key(app_env, tmp_path):
    rig = prepare(app_env, tmp_path)
    instance, transport = gateway(rig, (events(rig, b"malformed"),))

    def builder(result, ordinal):
        pytest.fail("Unexpected repair")

    instance.generate_with_repair(
        app_env.context,
        rig.node_id,
        rig.request,
        "fixed-policy",
        inference_ms=30000,
        repair_request=builder,
        policy=RepairPolicy(max_repairs=0),
    )
    with pytest.raises(RevisionConflictError):
        instance.generate_with_repair(
            app_env.context,
            rig.node_id,
            rig.request,
            "fixed-policy",
            inference_ms=30000,
            repair_request=builder,
            policy=RepairPolicy(max_repairs=1),
        )
    assert len(transport.requests) == 1


def test_non_fixture_transport_is_not_implicitly_enabled(app_env, tmp_path):
    rig = prepare(app_env, tmp_path)
    transport = ScriptedTransport((events(rig),))
    transport.fixture_only = False
    with pytest.raises(InvalidInputError):
        InferenceGateway(app_env.db, rig.artifacts, transport)
