"""Recipient contexts retain authoritative identities and actual source bytes."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from potbot.controller.execution import ExecutionStore
from potbot.domain.errors import (
    ContextOverflowError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.execution import ToolRegistry
from potbot.domain.serialization import canonical_json
from potbot.domain.tasks import ContractSpec
from potbot.models.context import (
    PACKET_POLICY,
    ArtifactContextSource,
    ContextPacketService,
    FixtureContextRenderer,
    MemoryBundle,
)
from potbot.models.invocations import (
    GenerateRequest,
    GenerationResult,
    InvocationStore,
    PacketSource,
)
from potbot.models.registry import DeploymentSpec, ModelRegistry
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.persistence.database import Database

from .test_coding_pipeline import setup

pytestmark = pytest.mark.postgres


def prepare(app_env, tmp_path, *, limit=100000, role="coding"):
    rig = setup(app_env, tmp_path)
    inputs = ExecutionStore(app_env.db, ToolRegistry(())).create_inputs(
        app_env.context, rig.episode_id, rig.contract_id, rig.state["id"], "context-inputs"
    )
    renderer = FixtureContextRenderer()
    schema = "coding-result-v1" if role == "coding" else "action-proposal-v1"
    spec = DeploymentSpec(
        renderer.canonical_model,
        "a" * 64,
        "fixture",
        "1",
        "none",
        renderer.tokenizer_revision,
        renderer.chat_template_digest,
        "json-envelope-v1",
        "none",
        limit,
        256,
        ((role, renderer.prompt_version),),
        (role,),
        (schema,),
    )
    deployed = ModelRegistry(app_env.db).register(app_env.context, spec)
    service = ContextPacketService(app_env.db, rig.artifacts)
    arguments = dict(
        deployment_id=deployed["id"],
        spec=spec,
        renderer=renderer,
        node_id=UUID(inputs["node_id"]),
        response_schema=schema,
        role=role,
        output_limit=128,
        implementation_id=UUID(rig.handoff["id"]) if role == "coding" else None,
    )
    return rig, service, arguments


def memory(artifacts, context, text, *, version="1", type_ref="synny://type/potbot-data-text"):
    saved = artifacts.put(context, text.encode(), "text/plain")
    return ArtifactContextSource(saved["id"], saved["digest"], type_ref, version)


def test_context_selects_full_current_handoff_and_retains_reproducible_provenance(
    app_env, tmp_path
):
    rig, service, arguments = prepare(app_env, tmp_path)
    first = service.create(app_env.context, **arguments, idempotency_key="packet")
    assert service.create(app_env.context, **arguments, idempotency_key="packet") == first
    manifest = first["manifest"]
    assert manifest["identity"]["contract_revision_id"] == str(rig.contract_id)
    assert manifest["baseline"]["implementation_id"] == rig.handoff["id"]
    assert manifest["baseline"]["knowledge_view_digest"]
    assert "Calculator tests pass" in manifest["wire_text"]
    assert "permitted_paths" in manifest["wire_text"]
    assert "def total" in manifest["wire_text"]
    assert manifest["input_tokens"] == first["token_count"]
    restarted_db = Database(app_env.db.dsn, app_env.schema)
    restarted = ContextPacketService(
        restarted_db, ArtifactStore(restarted_db, LocalArtifactBackend(app_env.artifact_root))
    )
    assert restarted.read(app_env.context, UUID(first["id"])) == manifest
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM model_invocations").fetchone()["n"] == 0
        sources = conn.execute(
            "SELECT artifact_id FROM context_packet_sources WHERE packet_id=%s ORDER BY ordinal",
            (UUID(first["id"]),),
        ).fetchall()
    assert [str(row["artifact_id"]) for row in sources] == list(
        dict.fromkeys(item["id"] for item in manifest["sources"])
    )
    for item in manifest["sources"]:
        assert item["type_ref"].startswith("synny://type/") and item["version"]
    with pytest.raises(RevisionConflictError):
        service.create(
            app_env.context, **{**arguments, "output_limit": 129}, idempotency_key="packet"
        )


def test_memory_qualification_is_atomic_and_required_memory_cannot_be_dropped(app_env, tmp_path):
    rig, service, arguments = prepare(app_env, tmp_path, limit=16000)
    large = memory(rig.artifacts, app_env.context, "UNSUITABLE_OVERSIZE_MEMORY_" * 10000)
    qualification = memory(
        rig.artifacts,
        app_env.context,
        "Only valid for Python 3.12; unverified model claim.",
        version="2",
    )
    small = memory(
        rig.artifacts, app_env.context, "A different model suggests checking operator precedence."
    )
    bundles = (
        MemoryBundle("oversized", large, (qualification,)),
        MemoryBundle("compact", small, (qualification,)),
    )
    packet = service.create(
        app_env.context, **arguments, optional_bundles=bundles, idempotency_key="optional"
    )
    manifest = packet["manifest"]
    assert manifest["included_optional_bundles"] == ["compact"]
    assert manifest["omissions"][0]["name"] == "oversized"
    assert "UNSUITABLE_OVERSIZE_MEMORY_" not in manifest["wire_text"]
    assert "Only valid for Python 3.12" in manifest["wire_text"]
    assert any(source["version"] == "2" for source in manifest["sources"])
    with pytest.raises(ContextOverflowError):
        service.create(
            app_env.context,
            **arguments,
            optional_bundles=(replace(bundles[0], required=True),),
            idempotency_key="required",
        )
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM context_packets").fetchone()["n"] == 1


def test_bad_source_digest_and_cross_workspace_sources_are_rejected(app_env, tmp_path):
    rig, service, arguments = prepare(app_env, tmp_path)
    source = memory(rig.artifacts, app_env.context, "Exact memory")
    with pytest.raises(InvalidInputError):
        service.create(
            app_env.context,
            **arguments,
            optional_bundles=(MemoryBundle("forged", replace(source, sha256="f" * 64)),),
            idempotency_key="forged",
        )
    other = ArtifactStore(app_env.other_db, LocalArtifactBackend(app_env.artifact_root))
    foreign = memory(other, app_env.other_context, "Private other workspace memory")
    with pytest.raises(ScopeDeniedError):
        service.create(
            app_env.context,
            **arguments,
            optional_bundles=(MemoryBundle("foreign", foreign),),
            idempotency_key="foreign",
        )
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM context_packets").fetchone()["n"] == 0


def test_revision_change_blocks_existing_context_dispatch_and_replay_but_allows_historical_read(
    app_env, tmp_path
):
    rig, service, arguments = prepare(app_env, tmp_path)
    packet = service.create(app_env.context, **arguments, idempotency_key="initial")
    with app_env.db.transaction(app_env.context) as conn:
        content = conn.execute(
            "SELECT content FROM contract_revisions WHERE id=%s", (rig.contract_id,)
        ).fetchone()["content"]
    contract = replace(
        ContractSpec.from_dict(content),
        compatibility_constraints=("Keep the public API unchanged.",),
    )
    rig.tasks.revise(app_env.context, rig.episode_id, rig.contract_id, contract, "new-revision")
    with pytest.raises(StateMismatchError):
        service.read(app_env.context, UUID(packet["id"]))
    with pytest.raises(StateMismatchError):
        service.create(app_env.context, **arguments, idempotency_key="initial")
    assert (
        service.read(app_env.context, UUID(packet["id"]), require_current=False)
        == packet["manifest"]
    )


def test_corrupted_retained_source_bytes_fail_at_dispatch_read(app_env, tmp_path):
    rig, service, arguments = prepare(app_env, tmp_path)
    packet = service.create(app_env.context, **arguments, idempotency_key="packet")
    source_id = UUID(packet["manifest"]["sources"][0]["id"])
    metadata = rig.artifacts.metadata(app_env.context, source_id)
    path = app_env.artifact_root / metadata["locator"]
    path.write_bytes(b"corrupted")
    with pytest.raises(InvalidInputError):
        service.read(app_env.context, UUID(packet["id"]))


def test_coding_context_rejects_wrong_base_and_missing_handoff(app_env, tmp_path):
    rig, service, arguments = prepare(app_env, tmp_path)
    with pytest.raises(InvalidInputError):
        service.create(
            app_env.context, **{**arguments, "implementation_id": None}, idempotency_key="missing"
        )
    (rig.fixture.path / "calculator.py").write_text("# changed base\n")
    new_state = rig.states.capture(
        app_env.context, app_env.repository_id, rig.fixture.path, rig.state["environment_id"]
    )
    new_inputs = ExecutionStore(app_env.db, ToolRegistry(())).create_inputs(
        app_env.context, rig.episode_id, rig.contract_id, new_state["id"], "different-base"
    )
    with pytest.raises(StateMismatchError):
        service.create(
            app_env.context,
            **{**arguments, "node_id": UUID(new_inputs["node_id"])},
            idempotency_key="wrong-base",
        )


def test_repair_packet_retains_previous_output_and_exact_baseline(app_env, tmp_path):
    rig, service, arguments = prepare(app_env, tmp_path)
    packet = service.create(app_env.context, **arguments, idempotency_key="first")
    raw = rig.artifacts.put(app_env.context, b'{"broken":', "text/plain")
    request = GenerateRequest(
        arguments["deployment_id"],
        "coding",
        UUID(packet["id"]),
        "coding-result-v1",
        '{"temperature":0}',
        128,
        datetime.now(UTC) + timedelta(minutes=2),
        uuid4(),
    )
    invalid = InvocationStore(app_env.db).record_final(
        app_env.context,
        request,
        GenerationResult(
            raw["id"],
            None,
            packet["token_count"],
            5,
            10,
            "stop",
            "invalid",
            "failed",
            "schema_violation",
        ),
    )
    repair = service.create(
        app_env.context, **arguments, repair_of=invalid["id"], idempotency_key="repair"
    )
    assert repair["id"] != packet["id"]
    assert repair["manifest"]["repair_of"] == str(invalid["id"])
    assert repair["manifest"]["identity"] == packet["manifest"]["identity"]
    assert str(raw["id"]) in [source["id"] for source in repair["manifest"]["sources"]]
    assert "response_format_repair" in repair["manifest"]["wire_text"]
    assert service.read(app_env.context, UUID(repair["id"])) == repair["manifest"]
    partial = InvocationStore(app_env.db).record_final(
        app_env.context,
        request,
        GenerationResult(
            raw["id"],
            None,
            packet["token_count"],
            5,
            10,
            "cancelled",
            "partial",
            "cancelled",
            "cancelled",
        ),
    )
    with pytest.raises(StateMismatchError):
        service.create(
            app_env.context, **arguments, repair_of=partial["id"], idempotency_key="bad-repair"
        )


@pytest.mark.parametrize(
    "damage", ["mixed_identity", "omitted_qualification", "false_semantic_validation"]
)
def test_context_read_rejects_inconsistent_identity_and_qualification_manifest(
    app_env, tmp_path, damage
):
    rig, service, arguments = prepare(app_env, tmp_path)
    qualification = memory(
        rig.artifacts, app_env.context, "Only valid on the measured Python 3.12 deployment."
    )
    source = replace(
        memory(rig.artifacts, app_env.context, "A model-authored memory."),
        qualifier_refs=(f"artifact:{qualification.artifact_id}",),
    )
    good = service.create(
        app_env.context,
        **arguments,
        optional_bundles=(MemoryBundle("qualified", source, (qualification,)),),
        idempotency_key="good",
    )
    manifest = json.loads(canonical_json(good["manifest"]))
    if damage == "mixed_identity":
        manifest["identity"]["node_id"] = str(uuid4())
    elif damage == "omitted_qualification":
        manifest["memory_bundles"][0]["qualifications"] = []
    else:
        manifest["sources"][0]["semantic_validation"] = "verified"
    artifact = rig.artifacts.put(
        app_env.context, canonical_json(manifest).encode(), "application/json"
    )
    forged = InvocationStore(app_env.db).create_packet(
        app_env.context,
        deployment_id=arguments["deployment_id"],
        artifact_id=artifact["id"],
        sources=tuple(
            dict.fromkeys(
                PacketSource("artifact", UUID(item["id"])) for item in manifest["sources"]
            )
        ),
        token_count=manifest["input_tokens"],
        policy_version=PACKET_POLICY,
        episode_id=rig.episode_id,
    )
    with pytest.raises(InvalidInputError):
        service.read(app_env.context, forged["id"])
