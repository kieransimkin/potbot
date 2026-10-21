"""Admitted inference connects to coding records without inventing verification."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from potbot.controller.execution import ExecutionStore
from potbot.controller.information_needs import NeedSpec
from potbot.controller.model_coding import ModelCodingService
from potbot.controller.verification import VerificationService
from potbot.domain.coding import CODING_RESPONSE_SCHEMA
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.execution import CapabilityContext, ToolDefinition, ToolRegistry
from potbot.domain.model_proposals import ModelCodingResponse
from potbot.domain.serialization import canonical_json
from potbot.domain.tasks import ContractSpec
from potbot.domain.verification import CheckDefinition, VerificationProfile
from potbot.models.context import ContextPacketService, FixtureContextRenderer
from potbot.models.gateway import InferenceGateway
from potbot.models.invocations import GenerateRequest, GenerationResult, InvocationStore
from potbot.models.registry import DeploymentSpec, ModelRegistry
from potbot.models.transport import ScriptedTransport, Terminal, TextDelta, TokenUsage
from potbot.tools.gateway import ToolGateway
from potbot.tools.receipts import RunReceiptStore

from .test_coding_pipeline import setup

pytestmark = pytest.mark.postgres


def prepare(app_env, tmp_path):
    rig = setup(app_env, tmp_path)
    with app_env.db.transaction(app_env.context) as conn:
        content = conn.execute(
            "SELECT content FROM contract_revisions WHERE id=%s", (rig.contract_id,)
        ).fetchone()["content"]
    contract = replace(
        ContractSpec.from_dict(content),
        budget=(
            ("tokens", 1_000_000),
            ("calls", 20),
            ("tool_ms", 300_000),
            ("concurrency", 4),
            ("inference_ms", 300_000),
            ("money_microunits", 0),
        ),
    )
    revised = rig.tasks.revise(
        app_env.context, rig.episode_id, rig.contract_id, contract, "model-budget"
    )
    rig.contract_id = UUID(revised["current_contract"]["id"])
    rig.handoff = rig.coding.create_contract(
        app_env.context, rig.episode_id, rig.contract_id, rig.state["id"], rig.spec, "model-handoff"
    )
    inputs = ExecutionStore(app_env.db, ToolRegistry(())).create_inputs(
        app_env.context, rig.episode_id, rig.contract_id, rig.state["id"], "model-inputs"
    )
    rig.node_id = UUID(inputs["node_id"])
    renderer = FixtureContextRenderer(canonical_model="potbot/model-coding-fixture")
    spec = DeploymentSpec(
        renderer.canonical_model,
        "a" * 64,
        "potbot-offline-fixture",
        "1",
        "none",
        renderer.tokenizer_revision,
        renderer.chat_template_digest,
        "json-envelope-v1",
        "none",
        100_000,
        8192,
        (("coding", renderer.prompt_version),),
        ("coding",),
        (CODING_RESPONSE_SCHEMA,),
    )
    deployed = ModelRegistry(app_env.db).register(app_env.context, spec)
    rig.renderer, rig.deployment, rig.deployment_spec = renderer, deployed, spec
    rig.packet = ContextPacketService(app_env.db, rig.artifacts).create(
        app_env.context,
        deployment_id=deployed["id"],
        spec=spec,
        renderer=renderer,
        node_id=rig.node_id,
        response_schema=CODING_RESPONSE_SCHEMA,
        role="coding",
        output_limit=4096,
        implementation_id=UUID(rig.handoff["id"]),
        idempotency_key="coding-packet",
    )
    rig.request = GenerateRequest(
        deployed["id"],
        "coding",
        UUID(rig.packet["id"]),
        CODING_RESPONSE_SCHEMA,
        '{"temperature":0}',
        4096,
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
    rig.model_coding = ModelCodingService(app_env.db, rig.artifacts)
    return rig


def generate(rig, response=None, *, settle=True, monkeypatch=None):
    value = rig.response if response is None else response
    raw = canonical_json(value.to_dict()).encode()
    transport = ScriptedTransport(
        (
            (
                TextDelta(raw),
                Terminal(usage=TokenUsage(rig.packet["token_count"], (len(raw) + 3) // 4)),
            ),
        )
    )
    gateway = InferenceGateway(rig.env.db, rig.artifacts, transport)
    if not settle:
        monkeypatch.setattr(
            gateway.budgets, "settle", lambda *args, **kwargs: {"status": "reserved"}
        )
    result = gateway.generate(
        rig.env.context, rig.node_id, rig.request, "coding-inference", inference_ms=60_000
    )
    assert result["status"] == "accepted"
    return result, transport


def test_accepted_model_output_preserves_gateway_provenance_and_replays_once(app_env, tmp_path):
    rig = prepare(app_env, tmp_path)
    inference, transport = generate(rig)
    result = rig.model_coding.record(
        app_env.context, UUID(rig.handoff["id"]), UUID(inference["invocation_id"]), "publish"
    )
    assert (
        rig.model_coding.record(
            app_env.context, UUID(rig.handoff["id"]), UUID(inference["invocation_id"]), "publish"
        )
        == result
    )
    assert (
        rig.model_coding.record(
            app_env.context,
            UUID(rig.handoff["id"]),
            UUID(inference["invocation_id"]),
            "another-key",
        )
        == result
    )
    assert result["content"]["producer_kind"] == "model"
    assert result["content"]["fixture_only"] is True
    assert result["content"]["verification_status"] == "unverified"
    assert result["invocation_id"] == inference["invocation_id"]
    assert result["reservation_id"] == inference["reservation_id"]
    assert rig.artifacts.read(app_env.context, UUID(result["patch_artifact_id"])) == rig.patch
    assert (rig.fixture.path / "calculator.py").read_text() == rig.source
    assert len(transport.requests) == 1
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM coding_results").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM candidate_workspaces").fetchone()["n"] == 0
        assert (
            conn.execute("SELECT status FROM episodes WHERE id=%s", (rig.episode_id,)).fetchone()[
                "status"
            ]
            == "running"
        )


def test_model_information_need_is_durable_open_and_idempotent(app_env, tmp_path):
    rig = prepare(app_env, tmp_path)
    need = NeedSpec(
        "Which input version should this workflow accept?",
        "Select compatible input parsing.",
        ("Version 1", "Version 2"),
        ("A versioned input example",),
        ("The selected input version matches the user's requirement",),
        "This task's workflow input",
    )
    response = replace(
        rig.response,
        patch=None,
        changed_files=(),
        inability_code="insufficient_context",
        inability_reason="Input compatibility is not yet specified.",
        information_needs=(need,),
    )
    inference, _ = generate(rig, response)
    result = rig.model_coding.record(
        app_env.context, UUID(rig.handoff["id"]), UUID(inference["invocation_id"]), "question"
    )
    assert (
        rig.model_coding.record(
            app_env.context, UUID(rig.handoff["id"]), UUID(inference["invocation_id"]), "question"
        )
        == result
    )
    assert result["patch_artifact_id"] is None
    assert result["inability_code"] == "insufficient_context"
    assert len(result["content"]["recorded_information_needs"]) == 1
    with app_env.db.transaction(app_env.context) as conn:
        rows = conn.execute(
            "SELECT n.episode_id,s.status,r.content FROM information_needs n JOIN need_status s ON (n.workspace_id,n.id)=(s.workspace_id,s.need_id) JOIN need_revisions r ON (r.workspace_id,r.id)=(s.workspace_id,s.current_revision_id)"
        ).fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "open"
    assert rows[0]["episode_id"] == rig.episode_id
    assert rows[0]["content"] == need.to_dict()


@pytest.mark.parametrize(
    "change,error",
    [
        ({"base_state_id": uuid4()}, StateMismatchError),
        ({"implementation_id": uuid4()}, StateMismatchError),
        ({"contract_revision_id": uuid4()}, StateMismatchError),
        ({"changed_files": ("outside.py",)}, ScopeDeniedError),
        ({"proposed_checks": ("invented-check",)}, ScopeDeniedError),
        ({"evidence_ids": (uuid4(),)}, ScopeDeniedError),
    ],
)
def test_structurally_valid_claims_still_require_controller_admission(
    app_env, tmp_path, change, error
):
    rig = prepare(app_env, tmp_path)
    inference, _ = generate(rig, replace(rig.response, **change))
    with pytest.raises(error):
        rig.model_coding.record(
            app_env.context, UUID(rig.handoff["id"]), UUID(inference["invocation_id"]), "reject"
        )
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM coding_results").fetchone()["n"] == 0


def test_unsettled_gateway_output_cannot_publish_coding_result(app_env, tmp_path, monkeypatch):
    rig = prepare(app_env, tmp_path)
    inference, _ = generate(rig, settle=False, monkeypatch=monkeypatch)
    assert inference["accounting"] == "reserved"
    with pytest.raises(StateMismatchError, match="not settled"):
        rig.model_coding.record(
            app_env.context, UUID(rig.handoff["id"]), UUID(inference["invocation_id"]), "unsettled"
        )


def test_stale_contract_blocks_both_publication_and_replay(app_env, tmp_path):
    rig = prepare(app_env, tmp_path)
    inference, _ = generate(rig)
    rig.model_coding.record(
        app_env.context, UUID(rig.handoff["id"]), UUID(inference["invocation_id"]), "publish"
    )
    with app_env.db.transaction(app_env.context) as conn:
        content = conn.execute(
            "SELECT content FROM contract_revisions WHERE id=%s", (rig.contract_id,)
        ).fetchone()["content"]
    changed = replace(
        ContractSpec.from_dict(content),
        compatibility_constraints=("Preserve Python 3.12 compatibility.",),
    )
    rig.tasks.revise(app_env.context, rig.episode_id, rig.contract_id, changed, "new-contract")
    for key in ("publish", "new-publication"):
        with pytest.raises(StateMismatchError):
            rig.model_coding.record(
                app_env.context, UUID(rig.handoff["id"]), UUID(inference["invocation_id"]), key
            )


def test_actual_raw_bytes_are_rechecked_before_result_publication(app_env, tmp_path):
    rig = prepare(app_env, tmp_path)
    inference, _ = generate(rig)
    metadata = rig.artifacts.metadata(app_env.context, UUID(inference["raw_output_artifact_id"]))
    (app_env.artifact_root / metadata["locator"]).write_bytes(b"corrupted output")
    with pytest.raises(InvalidInputError):
        rig.model_coding.record(
            app_env.context, UUID(rig.handoff["id"]), UUID(inference["invocation_id"]), "corrupt"
        )


def test_handcrafted_success_without_gateway_admission_is_rejected(app_env, tmp_path):
    rig = prepare(app_env, tmp_path)
    raw = rig.artifacts.put(
        app_env.context, canonical_json(rig.response.to_dict()).encode(), "application/json"
    )
    invocation = InvocationStore(app_env.db).record_final(
        app_env.context,
        rig.request,
        GenerationResult(
            raw["id"],
            canonical_json(rig.response.to_dict()),
            rig.packet["token_count"],
            1,
            0,
            "stop",
            "parsed",
            "succeeded",
        ),
    )
    with pytest.raises(StateMismatchError, match="gateway completion"):
        rig.model_coding.record(
            app_env.context, UUID(rig.handoff["id"]), invocation["id"], "forged"
        )


def test_coding_invocation_cannot_cross_workspace(app_env, tmp_path):
    rig = prepare(app_env, tmp_path)
    inference, _ = generate(rig)
    other = ModelCodingService(app_env.other_db, rig.artifacts)
    with pytest.raises(ScopeDeniedError):
        other.record(
            app_env.other_context,
            UUID(rig.handoff["id"]),
            UUID(inference["invocation_id"]),
            "cross-workspace",
        )


@pytest.mark.sandbox
def test_model_gateway_patch_applies_to_exact_base_and_passes_targeted_verification(
    app_env, tmp_path
):
    """Synthetic inference, real database, real isolated patch and repository tests."""
    rig = prepare(app_env, tmp_path)
    registry = ToolRegistry(
        (
            ToolDefinition(
                "calculator-check",
                "test",
                ("/usr/bin/python3", "-B", "-m", "unittest", "tests.test_calculator"),
                timeout_ms=5000,
            ),
        )
    )
    checks_gateway = ToolGateway(
        app_env.db,
        rig.artifacts,
        rig.candidates,
        registry,
        CapabilityContext(registry.inventory()),
        RunReceiptStore(tmp_path / "verification-receipts"),
    )
    verification = VerificationService(app_env.db, rig.artifacts, rig.candidates, checks_gateway)
    profile = verification.create_profile(
        app_env.context,
        rig.episode_id,
        rig.contract_id,
        rig.state["id"],
        VerificationProfile(
            "model-coding-acceptance",
            1,
            (
                CheckDefinition(
                    "calculator-tests",
                    "calculator-total",
                    "calculator-check",
                    ("R1",),
                    ("tests/test_calculator.py", "tests/__init__.py"),
                ),
            ),
        ),
        "model-verification-profile",
    )
    # Freeze a new handoff with the real installed check identity. The earlier
    # fixture handoff remains immutable and cannot borrow this profile later.
    rig.handoff = rig.coding.create_contract(
        app_env.context,
        rig.episode_id,
        rig.contract_id,
        rig.state["id"],
        replace(
            rig.spec,
            required_checks=tuple(
                (check["check_id"], check["definition_digest"])
                for check in profile["content"]["checks"]
            ),
        ),
        "verified-model-handoff",
    )
    rig.packet = ContextPacketService(app_env.db, rig.artifacts).create(
        app_env.context,
        deployment_id=rig.deployment["id"],
        spec=rig.deployment_spec,
        renderer=rig.renderer,
        node_id=rig.node_id,
        response_schema=CODING_RESPONSE_SCHEMA,
        role="coding",
        output_limit=rig.request.output_limit,
        implementation_id=UUID(rig.handoff["id"]),
        idempotency_key="verified-model-packet",
    )
    rig.request = replace(rig.request, context_packet_id=UUID(rig.packet["id"]))
    rig.response = replace(rig.response, implementation_id=UUID(rig.handoff["id"]))
    baseline = verification.start(
        app_env.context,
        UUID(profile["id"]),
        rig.state["id"],
        "baseline",
        "model-baseline-checks",
    )
    baseline_checked = verification.run_all(app_env.context, UUID(baseline["id"]))
    assert baseline_checked["checks"][0]["observation"]["status"] == "failed"

    inference, transport = generate(rig)
    result = rig.model_coding.record(
        app_env.context,
        UUID(rig.handoff["id"]),
        UUID(inference["invocation_id"]),
        "verified-model-result",
    )
    applied = rig.application.apply(app_env.context, UUID(result["id"]), "apply-model-result")
    assert applied["status"] == "applied"
    assert applied["verification_status"] == "unverified"
    assert applied["base_state_id"] == str(rig.state["id"])
    candidate_run = verification.start(
        app_env.context,
        UUID(profile["id"]),
        UUID(applied["result_state_id"]),
        "candidate",
        "model-candidate-checks",
        application_id=UUID(applied["id"]),
    )
    checked = verification.run_all(app_env.context, UUID(candidate_run["id"]))
    assert checked["checks"][0]["observation"]["status"] == "passed"
    assessment = verification.assess(
        app_env.context,
        UUID(applied["id"]),
        UUID(baseline["id"]),
        UUID(candidate_run["id"]),
        "model-criteria",
    )["content"]
    assert assessment["outcome"] == "verified"
    assert assessment["checks"][0]["comparison"] == "resolved"
    assert assessment["criteria"][0]["status"] == "passed"
    assert assessment["base_state_id"] == str(rig.state["id"])
    assert assessment["result_state_id"] == applied["result_state_id"]
    assert assessment["application_id"] == applied["id"]
    assert len(transport.requests) == 1
    candidate = rig.candidates.get(app_env.context, UUID(applied["candidate_id"]))
    assert (Path(candidate["path"]) / "calculator.py").read_text() != rig.source
    assert (rig.fixture.path / "calculator.py").read_text() == rig.source
