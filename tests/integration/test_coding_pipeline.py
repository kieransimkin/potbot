"""P036/P037 acceptance: real PostgreSQL provenance and sandboxed patch effects."""

import difflib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from potbot.controller.budgets import BudgetLedger
from potbot.controller.coding import CodingStore
from potbot.controller.reuse import plan_reuse_search, review_reuse_plan
from potbot.controller.tasks import TaskService
from potbot.domain.coding import ImplementationSpec, ScriptedCodingResponse
from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.execution import CapabilityContext, ToolRegistry
from potbot.domain.reuse import ReuseAction, ReuseDecision, ReuseScope, SearchEvidence, SearchStatus
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus
from potbot.evaluation.repository_fixture import (
    FAULTY_LINE,
    REPAIRED_LINE,
    create_repository_fixture,
)
from potbot.models.configuration import EnvironmentManifest, store_environment
from potbot.models.scripted import ScriptedCodingProducer
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.tools.gateway import ToolGateway
from potbot.tools.patch_application import (
    PatchApplicationService,
    patch_runtime_roots,
    patch_tool_definition,
)
from potbot.tools.receipts import RunReceiptStore
from potbot.tools.repository import CandidateWorkspaceManager, RepositoryStateStore

pytestmark = pytest.mark.postgres


def setup(app_env, tmp_path: Path):
    fixture = create_repository_fixture(tmp_path / "source")
    source = (fixture.path / "calculator.py").read_text()
    patch = "".join(
        difflib.unified_diff(
            source.splitlines(True),
            source.replace(FAULTY_LINE, REPAIRED_LINE).splitlines(True),
            fromfile="a/calculator.py",
            tofile="b/calculator.py",
        )
    ).encode()
    tasks = TaskService(app_env.db)
    contract = ContractSpec(
        "Repair the fixture",
        "git:" + fixture.baseline_commit,
        DeliverableType.PATCH,
        (Requirement("R1", "Total is correct", "Calculator tests pass"),),
        ("calculator.py", "tests"),
        ("patch", "test"),
        "fixture-v1",
        (("tokens", 100000), ("calls", 20), ("tool_ms", 300000), ("concurrency", 4)),
    )
    task = tasks.submit(app_env.context, app_env.repository_id, contract, "coding-task")
    episode_id, contract_id = UUID(task["task_id"]), UUID(task["current_contract"]["id"])
    with app_env.db.transaction(app_env.context) as conn:
        configuration = conn.execute(
            "SELECT * FROM resolved_configurations WHERE episode_id=%s", (episode_id,)
        ).fetchone()
        environment = store_environment(
            conn,
            app_env.context,
            EnvironmentManifest(
                configuration["id"],
                configuration["content_hash"],
                "linux",
                "python-fixture",
                (("git", "fixture"),),
            ),
        )
    artifacts = ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root))
    states = RepositoryStateStore(app_env.db, artifacts)
    state = states.capture(app_env.context, app_env.repository_id, fixture.path, environment["id"])
    initializing = tasks.transition(
        app_env.context,
        episode_id,
        0,
        TaskStatus.INITIALIZING,
        "Captured base",
        idempotency_key="initializing",
    )
    tasks.transition(
        app_env.context,
        episode_id,
        initializing["update_token"],
        TaskStatus.RUNNING,
        "Begin fixture",
        idempotency_key="running",
    )
    scope = ReuseScope(episode_id, app_env.repository_id, app_env.context.workspace_id)
    plan = plan_reuse_search(scope, "Repair calculator total using Python")
    receipts, searches = [], []
    for intent in plan.intents:
        receipt = artifacts.put(
            app_env.context,
            canonical_json(
                {
                    "origin": "provided_fixture",
                    "channel": intent.channel.value,
                    "status": "completed",
                    "results": [],
                }
            ).encode(),
            "application/json",
        )
        receipts.append(receipt["id"])
        searches.append(SearchEvidence(intent, SearchStatus.COMPLETED, f"artifact:{receipt['id']}"))
    assessment = review_reuse_plan(
        plan,
        searches,
        ReuseDecision(
            scope,
            ReuseAction.BUILD,
            "A one-line arithmetic correction needs no additional dependency.",
            (),
        ),
    )
    spec = ImplementationSpec(
        ("calculator.py",),
        ("The two existing calculator tests pass.",),
        (("calculator-tests", content_hash({"fixture": "tests/test_calculator.py"})),),
        assessment,
        tuple(receipts),
    )
    coding = CodingStore(app_env.db, artifacts)
    handoff = coding.create_contract(
        app_env.context, episode_id, contract_id, state["id"], spec, "handoff"
    )
    producer = ScriptedCodingProducer(app_env.db, artifacts)
    candidates = CandidateWorkspaceManager(app_env.db, artifacts, tmp_path / "candidates")
    registry = ToolRegistry((patch_tool_definition(),))
    gateway = ToolGateway(
        app_env.db,
        artifacts,
        candidates,
        registry,
        CapabilityContext(registry.inventory()),
        RunReceiptStore(tmp_path / "receipts"),
        read_only_roots=patch_runtime_roots(),
    )
    application = PatchApplicationService(app_env.db, artifacts, candidates, gateway)
    return SimpleNamespace(
        env=app_env,
        tasks=tasks,
        episode_id=episode_id,
        contract_id=contract_id,
        fixture=fixture,
        source=source,
        patch=patch,
        artifacts=artifacts,
        states=states,
        state=state,
        spec=spec,
        coding=coding,
        handoff=handoff,
        producer=producer,
        candidates=candidates,
        gateway=gateway,
        application=application,
    )


def produce(rig, key="response", patch=None):
    return rig.producer.produce(
        rig.env.context,
        ScriptedCodingResponse(
            UUID(rig.handoff["id"]),
            rig.state["id"],
            patch=rig.patch if patch is None else patch,
            assumptions=("Existing tests express the intended arithmetic.",),
        ),
        key,
    )


def test_scripted_output_has_exact_packet_sources_invocation_and_synthetic_usage(
    app_env, tmp_path: Path
) -> None:
    rig = setup(app_env, tmp_path)
    response = ScriptedCodingResponse(UUID(rig.handoff["id"]), rig.state["id"], patch=rig.patch)
    packet = "Exact caller-assembled packet. Preserve this text and its newline.\n"
    result = rig.producer.produce(app_env.context, response, "manual", packet_text=packet)
    assert rig.producer.produce(app_env.context, response, "manual", packet_text=packet) == result
    with pytest.raises(RevisionConflictError):
        rig.producer.produce(app_env.context, response, "manual", packet_text=packet + "changed")
    with app_env.db.transaction(app_env.context) as conn:
        invocation = conn.execute(
            "SELECT * FROM model_invocations WHERE id=%s", (UUID(result["invocation_id"]),)
        ).fetchone()
        frozen = conn.execute(
            "SELECT * FROM context_packets WHERE id=%s", (UUID(result["packet_id"]),)
        ).fetchone()
        source_ids = {
            row["artifact_id"]
            for row in conn.execute(
                "SELECT artifact_id FROM context_packet_sources WHERE packet_id=%s",
                (UUID(result["packet_id"]),),
            ).fetchall()
        }
        assert conn.execute("SELECT count(*) AS n FROM model_invocations").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM candidate_workspaces").fetchone()["n"] == 0
    assert invocation["usage_origin"] == "synthetic"
    assert invocation["elapsed_ms"] == 0 and invocation["stop_reason"] == "scripted_fixture"
    assert invocation["parsed_result"]["patch_artifact_id"] == result["patch_artifact_id"]
    assert rig.artifacts.read(app_env.context, frozen["artifact_id"]) == packet.encode()
    assert source_ids == set(rig.spec.search_receipt_ids) | {rig.state["snapshot_artifact_id"]}
    assert (
        json.loads(rig.artifacts.read(app_env.context, invocation["raw_output_artifact_id"]))[
            "patch"
        ].encode()
        == rig.patch
    )
    budget = BudgetLedger(app_env.db).inspect(app_env.context, rig.episode_id)
    assert budget["used"]["calls"] == 1 and budget["used"]["tokens"] > 0
    assert not budget["held"].get("concurrency", 0)


def test_unknown_handoff_wrong_base_inability_and_cross_workspace_never_allocate_candidates(
    app_env, tmp_path: Path
) -> None:
    rig = setup(app_env, tmp_path)
    with pytest.raises(ScopeDeniedError):
        rig.producer.produce(
            app_env.context,
            ScriptedCodingResponse(uuid4(), rig.state["id"], patch=rig.patch),
            "unknown",
        )
    with pytest.raises(StateMismatchError):
        rig.producer.produce(
            app_env.context,
            ScriptedCodingResponse(UUID(rig.handoff["id"]), uuid4(), patch=rig.patch),
            "wrong-base",
        )
    inability = rig.producer.produce(
        app_env.context,
        ScriptedCodingResponse(
            UUID(rig.handoff["id"]),
            rig.state["id"],
            inability_code="insufficient_context",
            inability_reason="Fixture response explicitly declines.",
        ),
        "inability",
    )
    assert inability["patch_artifact_id"] is None
    with pytest.raises(InvalidInputError):
        rig.application.apply(app_env.context, UUID(inability["id"]), "cannot-apply")
    other = CodingStore(
        app_env.other_db,
        ArtifactStore(app_env.other_db, LocalArtifactBackend(app_env.artifact_root)),
    )
    with pytest.raises(ScopeDeniedError):
        other.get_contract(app_env.other_context, UUID(rig.handoff["id"]))
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM candidate_workspaces").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM coding_results").fetchone()["n"] == 1


def test_reuse_receipts_and_task_scope_are_enforced(app_env, tmp_path: Path) -> None:
    rig = setup(app_env, tmp_path)
    for spec, error in [
        (replace(rig.spec, permitted_paths=("outside.py",)), ScopeDeniedError),
        (replace(rig.spec, search_receipt_ids=(uuid4(),)), InvalidInputError),
    ]:
        with pytest.raises(error):
            rig.coding.create_contract(
                app_env.context, rig.episode_id, rig.contract_id, rig.state["id"], spec, uuid4().hex
            )
    receipt = rig.artifacts.metadata(app_env.context, rig.spec.search_receipt_ids[0])
    assert rig.handoff["content"]["search_receipts"][0]["digest"] == receipt["digest"]
    assert rig.handoff["content"]["spec"]["discovery_origin"] == "provided_receipts"


@pytest.mark.sandbox
def test_patch_applies_in_isolation_replays_and_explicit_import_stays_unverified(
    app_env, tmp_path: Path
) -> None:
    rig = setup(app_env, tmp_path)
    result = produce(rig)
    applied = rig.application.apply(app_env.context, UUID(result["id"]), "apply")
    assert applied["status"] == "applied", applied
    assert applied["verification_status"] == "unverified"
    candidate = rig.candidates.get(app_env.context, UUID(applied["candidate_id"]))
    assert candidate["status"] == "ready"
    assert REPAIRED_LINE in (Path(candidate["path"]) / "calculator.py").read_text()
    assert (rig.fixture.path / "calculator.py").read_text() == rig.source
    assert rig.application.apply(app_env.context, UUID(result["id"]), "apply") == applied
    imported_candidate = rig.candidates.allocate(app_env.context, rig.state["id"])
    imported = rig.application.apply(
        app_env.context,
        UUID(result["id"]),
        "import",
        candidate_id=imported_candidate["id"],
        imported_from=f"application:{applied['id']}",
    )
    assert imported["status"] == "applied" and imported["verification_status"] == "unverified"
    assert imported["run_id"] != applied["run_id"]
    assert imported["candidate_id"] != applied["candidate_id"]
    assert imported["imported_from"] == f"application:{applied['id']}"
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM action_runs").fetchone()["n"] == 2
    assert BudgetLedger(app_env.db).inspect(app_env.context, rig.episode_id)["used"]["calls"] == 3


@pytest.mark.sandbox
def test_out_of_scope_patch_has_no_successful_application(app_env, tmp_path: Path) -> None:
    rig = setup(app_env, tmp_path)
    patch = b"diff --git a/outside.py b/outside.py\nnew file mode 100644\n--- /dev/null\n+++ b/outside.py\n@@ -0,0 +1 @@\n+unexpected\n"
    result = produce(rig, patch=patch)
    applied = rig.application.apply(app_env.context, UUID(result["id"]), "out-of-scope")
    assert applied["status"] == "failed"
    assert applied["details"]["worker_report"]["error_code"] == "scope_denied"
    assert (
        rig.states.get(app_env.context, UUID(applied["result_state_id"]))["content_digest"]
        == rig.state["content_digest"]
    )
    candidate = rig.candidates.get(app_env.context, UUID(applied["candidate_id"]))
    assert not (Path(candidate["path"]) / "outside.py").exists()


@pytest.mark.sandbox
def test_unrecorded_changed_base_never_dispatches_a_patch(app_env, tmp_path: Path) -> None:
    rig = setup(app_env, tmp_path)
    result = produce(rig)
    candidate = rig.candidates.allocate(app_env.context, rig.state["id"])
    (Path(candidate["path"]) / "calculator.py").write_text("# changed outside its state identity\n")
    with pytest.raises(StateMismatchError):
        rig.application.apply(
            app_env.context,
            UUID(result["id"]),
            "changed",
            candidate_id=candidate["id"],
            imported_from="test-fixture:changed-base",
        )
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM action_runs").fetchone()["n"] == 0
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM patch_applications WHERE status='applied'"
            ).fetchone()["n"]
            == 0
        )
    assert (rig.fixture.path / "calculator.py").read_text() == rig.source


@pytest.mark.sandbox
@pytest.mark.parametrize("boundary", ["receipt", "application_record"])
def test_patch_resume_recognizes_durable_execution_without_reapplying(
    app_env, tmp_path: Path, monkeypatch, boundary: str
) -> None:
    rig = setup(app_env, tmp_path)
    result = produce(rig)

    def crash(*args, **kwargs):
        raise RuntimeError("injected controller loss")

    if boundary == "receipt":
        original = rig.gateway.execute

        def injected(*args, **kwargs):
            return original(*args, **kwargs, after_receipt=crash)

        monkeypatch.setattr(rig.gateway, "execute", injected)
    else:
        monkeypatch.setattr(rig.application, "_finish", crash)
    with pytest.raises(RuntimeError, match="injected controller loss"):
        rig.application.apply(app_env.context, UUID(result["id"]), "recoverable")
    restored = PatchApplicationService(app_env.db, rig.artifacts, rig.candidates, rig.gateway)
    if boundary == "receipt":
        with pytest.raises(IndeterminateExecutionError):
            restored.apply(app_env.context, UUID(result["id"]), "recoverable")
        with app_env.db.transaction(app_env.context) as conn:
            run_id = conn.execute("SELECT id FROM action_runs").fetchone()["id"]
        assert (
            rig.gateway.reconcile(app_env.context, run_id)["completion"]["outcome"] == "completed"
        )
    application = restored.apply(app_env.context, UUID(result["id"]), "recoverable")
    assert application["status"] == "applied"
    assert application["verification_status"] == "unverified"
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM action_runs").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM patch_applications").fetchone()["n"] == 1
    assert BudgetLedger(app_env.db).inspect(app_env.context, rig.episode_id)["used"]["calls"] == 2


def test_scripted_committed_result_is_settled_after_interruption_without_new_invocation(
    app_env, tmp_path: Path, monkeypatch
) -> None:
    rig = setup(app_env, tmp_path)
    response = ScriptedCodingResponse(UUID(rig.handoff["id"]), rig.state["id"], patch=rig.patch)

    def crash(*args, **kwargs):
        raise RuntimeError("injected settlement loss")

    monkeypatch.setattr(rig.producer, "_settle", crash)
    with pytest.raises(RuntimeError, match="injected settlement loss"):
        rig.producer.produce(app_env.context, response, "settlement")
    restored = ScriptedCodingProducer(app_env.db, rig.artifacts)
    result = restored.produce(app_env.context, response, "settlement")
    assert restored.produce(app_env.context, response, "settlement") == result
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM model_invocations").fetchone()["n"] == 1
    budget = BudgetLedger(app_env.db).inspect(app_env.context, rig.episode_id)
    assert budget["used"]["calls"] == 1
    assert not budget["held"].get("concurrency", 0)
