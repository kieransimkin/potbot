"""P039/P040 acceptance for the durable task-to-patch workflow and exports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from potbot.controller.patch_pipeline import PatchPipeline
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.execution import CapabilityContext
from potbot.evaluation.patch_fixture import (
    fixture_contract,
    fixture_patch,
    fixture_profile,
    fixture_tools,
)
from potbot.evaluation.repository_fixture import FixtureVariant, create_repository_fixture
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.tools.gateway import ToolGateway
from potbot.tools.patch_application import PatchApplicationService, patch_runtime_roots
from potbot.tools.receipts import RunReceiptStore
from potbot.tools.repository import CandidateWorkspaceManager

pytestmark = [pytest.mark.postgres, pytest.mark.sandbox]


def _controller(app_env, tmp_path: Path, artifacts: ArtifactStore):
    candidates = CandidateWorkspaceManager(app_env.db, artifacts, tmp_path / "candidates")
    registry = fixture_tools()
    gateway = ToolGateway(
        app_env.db,
        artifacts,
        candidates,
        registry,
        CapabilityContext(registry.inventory()),
        RunReceiptStore(tmp_path / "receipts"),
        read_only_roots=patch_runtime_roots(),
    )
    return PatchPipeline(app_env.db, artifacts, candidates, gateway)


def setup(app_env, tmp_path: Path, *, preexisting=False, partial=False):
    fixture = create_repository_fixture(
        tmp_path / "source",
        FixtureVariant.PRE_EXISTING_FAILURE if preexisting else FixtureVariant.BASELINE,
    )
    assert fixture.baseline_commit is not None
    contract, profile = (
        fixture_contract(fixture.baseline_commit),
        fixture_profile(preexisting=preexisting),
    )
    patch = fixture_patch(fixture.path)
    if partial:
        patch = patch.replace(
            b"+    return sum(values) + offset\n",
            b"+    return sum(values) + offset - (not values)\n",
        )
    artifacts = ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root))
    pipeline = _controller(app_env, tmp_path, artifacts)
    created = pipeline.create(
        app_env.context,
        app_env.repository_id,
        fixture.path,
        contract,
        profile,
        patch,
        "vertical-slice",
    )
    return SimpleNamespace(
        env=app_env,
        fixture=fixture,
        contract=contract,
        profile=profile,
        patch=patch,
        source=(fixture.path / "calculator.py").read_bytes(),
        artifacts=artifacts,
        pipeline=pipeline,
        created=created,
        pipeline_id=UUID(created["id"]),
        tmp_path=tmp_path,
    )


def counts(rig):
    with rig.env.db.transaction(rig.env.context) as conn:
        return tuple(
            conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
            for table in (
                "action_runs",
                "model_invocations",
                "patch_applications",
                "patch_exports",
            )
        )


def test_complete_vertical_slice_freezes_criterion_scope_and_actual_participant_provenance(
    app_env, tmp_path: Path
) -> None:
    rig = setup(app_env, tmp_path)
    report = rig.pipeline.resume(app_env.context, rig.pipeline_id)
    assert report["checkpoint"] == "export" and report["outcome"] == "verified", report
    assert report["task"]["lifecycle"] == "verified"
    manifest = report["export"]["manifest"]
    assert manifest["base_state"]["id"] == rig.created["base_state_id"]
    assert manifest["result_state"]["id"] == manifest["application"]["result_state_id"]
    assert manifest["base_state"]["content_digest"] != manifest["result_state"]["content_digest"]
    assert manifest["implementation_contract"]["content"]["spec"]["permitted_paths"] == list(
        rig.contract.change_scope
    )
    criteria = {entry["id"]: entry for entry in manifest["criteria"]}
    assert {key: entry["check_ids"] for key, entry in criteria.items()} == {
        "R1": ["total"],
        "R2": ["empty"],
    }
    assert all(entry["status"] == "passed" for entry in criteria.values())
    checks = {entry["check_id"]: entry for entry in manifest["checks"]}
    assert checks["compile"]["comparison"] == "still_passed"
    assert checks["total"]["comparison"] == checks["empty"]["comparison"] == "resolved"
    assert checks["total"]["baseline_status"] == checks["empty"]["baseline_status"] == "failed"
    participants = manifest["participating_deployments"]
    assert len(participants) == 1
    assert participants[0]["canonical_model"] == "potbot/scripted-patch-fixture"
    assert (
        participants[0]["id"]
        == manifest["coding"]["result"]["deployment_id"]
        == manifest["coding"]["invocation"]["deployment_id"]
    )
    assert manifest["coding"]["invocation"]["usage_origin"] == "synthetic"
    assert (
        manifest["coding"]["packet"]["id"] == manifest["coding"]["invocation"]["context_packet_id"]
    )
    inventory = {entry["id"]: entry for entry in manifest["artifacts"]}
    assert manifest["coding"]["packet"]["artifact_id"] in inventory
    assert manifest["coding"]["invocation"]["raw_output_artifact_id"] in inventory
    assert manifest["patch"]["sha256"] == hashlib.sha256(rig.patch).hexdigest()
    assert all(
        entry["run"]["contract_revision_id"] == manifest["contract"]["id"]
        for entry in manifest["executions"]
    )
    assert all(
        entry["run"]["workspace_id"] == str(app_env.context.workspace_id)
        for entry in manifest["executions"]
    )
    assert (rig.fixture.path / "calculator.py").read_bytes() == rig.source
    assert counts(rig) == (7, 1, 1, 1)
    assert rig.pipeline.resume(app_env.context, rig.pipeline_id) == report
    assert (
        rig.pipeline.create(
            app_env.context,
            app_env.repository_id,
            rig.fixture.path,
            rig.contract,
            rig.profile,
            rig.patch,
            "vertical-slice",
        )
        == rig.created
    )
    assert counts(rig) == (7, 1, 1, 1)
    with pytest.raises(ScopeDeniedError):
        with app_env.db.transaction(app_env.context) as conn:
            conn.execute(
                "UPDATE patch_exports SET manifest_digest=%s WHERE id=%s",
                ("0" * 64, UUID(report["export"]["id"])),
            )


def test_optional_preexisting_failure_remains_failed_and_visible(app_env, tmp_path: Path) -> None:
    rig = setup(app_env, tmp_path, preexisting=True)
    report = rig.pipeline.resume(app_env.context, rig.pipeline_id)
    manifest = report["export"]["manifest"]
    # It does not satisfy a requirement and was explicitly optional. Its failure
    # remains visible while all mandatory acceptance criteria are satisfied.
    assert report["outcome"] == "verified"
    unrelated = next(entry for entry in manifest["checks"] if entry["check_id"] == "preexisting")
    assert unrelated["mandatory"] is False
    assert (
        unrelated["status"]
        == unrelated["baseline_status"]
        == unrelated["candidate_status"]
        == "failed"
    )
    assert unrelated["comparison"] == "pre_existing_failure"
    assert any(
        "preexisting: failed (pre_existing_failure)" in limitation
        for limitation in manifest["limitations"]
    )


@pytest.mark.parametrize(
    "checkpoint,expected", [("baseline", (3, 0, 0, 0)), ("application", (4, 1, 1, 0))]
)
def test_restart_after_checkpoint_does_not_duplicate_completed_work(
    app_env, tmp_path: Path, checkpoint: str, expected: tuple
) -> None:
    rig = setup(app_env, tmp_path)
    first = rig.pipeline.resume(app_env.context, rig.pipeline_id, stop_after=checkpoint)
    assert first["checkpoint"] == checkpoint and counts(rig) == expected
    restored = _controller(app_env, tmp_path, rig.artifacts)
    assert restored.resume(app_env.context, rig.pipeline_id, stop_after=checkpoint) == first
    assert counts(rig) == expected
    completed = restored.resume(app_env.context, rig.pipeline_id)
    assert completed["outcome"] == "verified" and counts(rig) == (7, 1, 1, 1)
    assert (
        _controller(app_env, tmp_path, rig.artifacts).resume(app_env.context, rig.pipeline_id)
        == completed
    )
    assert counts(rig) == (7, 1, 1, 1)


def test_contract_revision_after_verification_prevents_finalization(
    app_env, tmp_path: Path
) -> None:
    rig = setup(app_env, tmp_path)
    checked = rig.pipeline.resume(app_env.context, rig.pipeline_id, stop_after="verification")
    assert checked["outcome"] == "verified"
    changed = replace(rig.contract, objective="Revised acceptance task after the recorded checks")
    rig.pipeline.tasks.revise(
        app_env.context,
        UUID(rig.created["episode_id"]),
        UUID(rig.created["contract_revision_id"]),
        changed,
        "revised",
    )
    for _ in range(2):
        with pytest.raises(StateMismatchError):
            rig.pipeline.resume(app_env.context, rig.pipeline_id)
    with pytest.raises(StateMismatchError):
        rig.pipeline.exports.create(
            app_env.context, UUID(checked["assessment"]["id"]), "stale-assessment"
        )
    assert counts(rig) == (7, 1, 1, 0)
    inspected = rig.pipeline.inspect(app_env.context, rig.pipeline_id)
    needs = inspected["reassessment_needs"]
    assert len(needs) == 1 and needs[0]["status"] == "open"
    assert inspected["task"]["current_contract"]["id"] in needs[0]["content"]["scope"]
    assert "Fresh baseline and candidate checks" in " ".join(
        needs[0]["content"]["required_evidence"]
    )
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM information_needs").fetchone()["n"] == 1
    assert (
        rig.pipeline.tasks.inspect(app_env.context, UUID(rig.created["episode_id"]))["lifecycle"]
        == "running"
    )


def test_same_exact_import_requires_fresh_application_bound_checks(app_env, tmp_path: Path) -> None:
    rig = setup(app_env, tmp_path)
    checked = rig.pipeline.resume(app_env.context, rig.pipeline_id, stop_after="verification")
    application = checked["application"]
    target = rig.pipeline.candidates.allocate(app_env.context, UUID(rig.created["base_state_id"]))
    imported = PatchApplicationService(
        app_env.db, rig.artifacts, rig.pipeline.candidates, rig.pipeline.gateway
    ).apply(
        app_env.context,
        UUID(checked["coding"]["id"]),
        "import",
        candidate_id=target["id"],
        imported_from=f"application:{application['id']}",
    )
    assert imported["result_state_id"] == application["result_state_id"]
    assert imported["id"] != application["id"] and imported["run_id"] != application["run_id"]
    with pytest.raises(StateMismatchError):
        rig.pipeline.verification.assess(
            app_env.context,
            UUID(imported["id"]),
            UUID(checked["baseline"]["id"]),
            UUID(checked["verification"]["id"]),
            "borrowed-checks",
        )
    fresh = rig.pipeline.verification.start(
        app_env.context,
        UUID(rig.created["profile_id"]),
        UUID(imported["result_state_id"]),
        "import",
        "import-verification",
        application_id=UUID(imported["id"]),
    )
    completed = rig.pipeline.verification.run_all(app_env.context, UUID(fresh["id"]))
    old_executions = {
        entry["observation"]["execution_run_id"] for entry in checked["verification"]["checks"]
    }
    new_executions = {entry["observation"]["execution_run_id"] for entry in completed["checks"]}
    assert len(new_executions) == 3 and old_executions.isdisjoint(new_executions)
    assessed = rig.pipeline.verification.assess(
        app_env.context,
        UUID(imported["id"]),
        UUID(checked["baseline"]["id"]),
        UUID(fresh["id"]),
        "fresh-import-checks",
    )
    assert assessed["content"]["outcome"] == "verified"


def test_partially_correct_patch_exports_partial_with_failed_criterion(
    app_env, tmp_path: Path
) -> None:
    rig = setup(app_env, tmp_path, partial=True)
    report = rig.pipeline.resume(app_env.context, rig.pipeline_id)
    assert report["outcome"] == "partial" and report["task"]["lifecycle"] == "partial"
    criteria = {entry["id"]: entry["status"] for entry in report["export"]["manifest"]["criteria"]}
    assert criteria == {"R1": "passed", "R2": "failed"}


def test_not_run_checks_export_explicit_blocked_criteria_without_verified_claim(
    app_env, tmp_path: Path
) -> None:
    rig = setup(app_env, tmp_path)
    applied = rig.pipeline.resume(app_env.context, rig.pipeline_id, stop_after="application")
    application = applied["application"]
    unrun = rig.pipeline.verification.start(
        app_env.context,
        UUID(rig.created["profile_id"]),
        UUID(application["result_state_id"]),
        "candidate",
        "unrun",
        application_id=UUID(application["id"]),
    )
    assessment = rig.pipeline.verification.assess(
        app_env.context,
        UUID(application["id"]),
        UUID(applied["baseline"]["id"]),
        UUID(unrun["id"]),
        "unrun-assessment",
    )
    assert assessment["content"]["outcome"] == "blocked"
    exported = rig.pipeline.exports.create(app_env.context, UUID(assessment["id"]), "unrun-export")
    manifest = rig.pipeline.exports.inspect(app_env.context, UUID(exported["id"]))["manifest"]
    assert manifest["outcome"] == "blocked"
    assert all(entry["status"] == "not_run" for entry in manifest["criteria"])
    assert all(
        entry["candidate_status"] == "not_run" and entry["execution_run_id"] is None
        for entry in manifest["checks"]
    )
    assert (
        rig.pipeline.tasks.inspect(app_env.context, UUID(rig.created["episode_id"]))["lifecycle"]
        == "running"
    )


def test_cleanup_preserves_export_artifacts_and_idempotent_bundle(app_env, tmp_path: Path) -> None:
    rig = setup(app_env, tmp_path)
    report = rig.pipeline.resume(app_env.context, rig.pipeline_id)
    exported = report["export"]
    with app_env.db.transaction(app_env.context) as conn:
        candidate_ids = [
            row["id"] for row in conn.execute("SELECT id FROM candidate_workspaces").fetchall()
        ]
    assert candidate_ids
    for identity in candidate_ids:
        rig.pipeline.candidates.cleanup(app_env.context, identity)
    assert rig.pipeline.exports.inspect(app_env.context, UUID(exported["id"])) == exported
    directory = tmp_path / "export"
    bundle = rig.pipeline.exports.write_bundle(app_env.context, UUID(exported["id"]), directory)
    assert (
        rig.pipeline.exports.write_bundle(app_env.context, UUID(exported["id"]), directory)
        == bundle
    )
    assert (directory / "candidate.patch").read_bytes() == rig.patch
    assert json.loads((directory / "manifest.json").read_bytes()) == exported["manifest"]
    assert {name for name in bundle["files"]} == {"candidate.patch", "manifest.json", "SHA256SUMS"}
    assert all(
        hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest
        for name, digest in bundle["files"].items()
    )
    assert (rig.fixture.path / "calculator.py").read_bytes() == rig.source


def test_corrupt_linked_artifact_blocks_publication_and_retained_export_reads(
    app_env, tmp_path: Path
) -> None:
    rig = setup(app_env, tmp_path)
    checked = rig.pipeline.resume(app_env.context, rig.pipeline_id, stop_after="verification")
    with app_env.db.transaction(app_env.context) as conn:
        raw_id = conn.execute(
            "SELECT raw_output_artifact_id FROM model_invocations WHERE id=%s",
            (UUID(checked["coding"]["invocation_id"]),),
        ).fetchone()["raw_output_artifact_id"]

    def corrupt(identity):
        metadata = rig.artifacts.metadata(app_env.context, identity)
        target = app_env.artifact_root / metadata["locator"]
        original = target.read_bytes()
        target.write_bytes(original + b"\ncorrupted fixture artifact\n")
        return target, original

    target, original = corrupt(raw_id)
    try:
        with pytest.raises(InvalidInputError):
            rig.pipeline.exports.create(
                app_env.context, UUID(checked["assessment"]["id"]), "corrupt-publication"
            )
    finally:
        target.write_bytes(original)
    assert counts(rig)[3] == 0
    exported = rig.pipeline.exports.create(
        app_env.context, UUID(checked["assessment"]["id"]), "clean-publication"
    )
    inspected = rig.pipeline.exports.inspect(app_env.context, UUID(exported["id"]))
    identities = (
        UUID(exported["patch_artifact_id"]),
        UUID(exported["manifest_artifact_id"]),
        raw_id,
        UUID(inspected["manifest"]["coding"]["packet"]["artifact_id"]),
    )
    for identity in identities:
        target, original = corrupt(identity)
        try:
            with pytest.raises(InvalidInputError):
                rig.pipeline.exports.inspect(app_env.context, UUID(exported["id"]))
            with pytest.raises(InvalidInputError):
                rig.pipeline.exports.write_bundle(
                    app_env.context, UUID(exported["id"]), tmp_path / "blocked-export"
                )
        finally:
            target.write_bytes(original)
    assert not (tmp_path / "blocked-export").exists()
    assert rig.pipeline.exports.inspect(app_env.context, UUID(exported["id"])) == inspected


@pytest.mark.parametrize("boundary", ["before_inspection", "after_inspection"])
def test_revision_during_finalization_cannot_verify_a_new_contract(
    app_env, tmp_path: Path, monkeypatch, boundary: str
) -> None:
    rig = setup(app_env, tmp_path)
    rig.pipeline.resume(app_env.context, rig.pipeline_id, stop_after="verification")
    changed = replace(rig.contract, objective="A new contract arrived during finalization")

    def revise():
        rig.pipeline.tasks.revise(
            app_env.context,
            UUID(rig.created["episode_id"]),
            UUID(rig.created["contract_revision_id"]),
            changed,
            "late-revision",
        )

    if boundary == "before_inspection":
        original = rig.pipeline.exports.create

        def inject(*args, **kwargs):
            result = original(*args, **kwargs)
            revise()
            return result

        monkeypatch.setattr(rig.pipeline.exports, "create", inject)
    else:
        original = rig.pipeline.exports.inspect

        def inject(*args, **kwargs):
            result = original(*args, **kwargs)
            revise()
            return result

        monkeypatch.setattr(rig.pipeline.exports, "inspect", inject)
    with pytest.raises(StateMismatchError):
        rig.pipeline.resume(app_env.context, rig.pipeline_id)
    task = rig.pipeline.tasks.inspect(app_env.context, UUID(rig.created["episode_id"]))
    assert task["lifecycle"] == "running"
    assert task["current_contract"]["id"] != rig.created["contract_revision_id"]
    assert counts(rig) == (7, 1, 1, 1)


def test_unrelated_task_export_is_not_adopted_as_pipeline_completion(
    app_env, tmp_path: Path
) -> None:
    rig = setup(app_env, tmp_path)
    checked = rig.pipeline.resume(app_env.context, rig.pipeline_id, stop_after="verification")
    # An explicitly incomplete independent run on the same application may
    # produce a reviewable blocked export, but does not finalize this pipeline.
    unrun = rig.pipeline.verification.start(
        app_env.context,
        UUID(rig.created["profile_id"]),
        UUID(checked["application"]["result_state_id"]),
        "candidate",
        "independent-unrun",
        application_id=UUID(checked["application"]["id"]),
    )
    assessment = rig.pipeline.verification.assess(
        app_env.context,
        UUID(checked["application"]["id"]),
        UUID(checked["baseline"]["id"]),
        UUID(unrun["id"]),
        "independent-assessment",
    )
    independent = rig.pipeline.exports.create(
        app_env.context, UUID(assessment["id"]), "independent-export"
    )
    assert (
        rig.pipeline.exports.inspect(app_env.context, UUID(independent["id"]))["manifest"][
            "outcome"
        ]
        == "blocked"
    )
    assert rig.pipeline.inspect(app_env.context, rig.pipeline_id)["exports"] == []
    completed = rig.pipeline.resume(app_env.context, rig.pipeline_id)
    assert completed["outcome"] == "verified"
    assert completed["export"]["id"] != independent["id"]
    assert completed["export"]["assessment_id"] == checked["assessment"]["id"]
    assert counts(rig) == (7, 1, 1, 2)
