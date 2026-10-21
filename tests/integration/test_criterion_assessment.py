"""P038 criterion authority and regression decisions through real patch exports."""

from __future__ import annotations

import difflib
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from potbot.evaluation.patch_fixture import fixture_contract, fixture_patch, fixture_profile
from potbot.evaluation.repository_fixture import (
    REPAIRED_LINE,
    create_repository_fixture,
    repair_repository_fixture,
)
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend

from .conftest import AppEnvironment
from .test_patch_vertical_slice import _controller

pytestmark = [pytest.mark.postgres, pytest.mark.sandbox]


def test_agent_authored_passes_do_not_verify_their_own_or_unrelated_criteria(
    app_env: AppEnvironment,
    tmp_path: Path,
) -> None:
    fixture = create_repository_fixture(tmp_path / "source")
    assert fixture.baseline_commit is not None
    contract = fixture_contract(fixture.baseline_commit)
    original = fixture_profile()
    # Only R1 has a behavior check, and the check's authority is explicitly
    # agent-authored. R2 must not borrow the compiler or R1's passing result.
    profile = replace(
        original,
        checks=(
            original.checks[0],
            replace(original.checks[1], authored_by="agent"),
        ),
    )
    artifacts = ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root))
    pipeline = _controller(app_env, tmp_path, artifacts)
    created = pipeline.create(
        app_env.context,
        app_env.repository_id,
        fixture.path,
        contract,
        profile,
        fixture_patch(fixture.path),
        "criterion-authority",
    )
    report = pipeline.resume(app_env.context, UUID(created["id"]))
    manifest = report["export"]["manifest"]
    checks = {check["check_id"]: check for check in manifest["checks"]}
    assert checks["compile"]["status"] == "passed"
    assert checks["total"]["status"] == "passed"
    assert checks["total"]["baseline_status"] == "failed"
    assert checks["total"]["candidate_status"] == "passed"
    assert checks["total"]["authored_by"] == "agent"
    assert checks["total"]["comparison"] == "resolved"
    criteria = {criterion["id"]: criterion for criterion in manifest["criteria"]}
    assert criteria["R1"]["status"] == "inconclusive"
    assert criteria["R1"]["check_ids"] == ["total"]
    assert criteria["R2"]["status"] == "not_run"
    assert criteria["R2"]["check_ids"] == []
    assert any("Agent-authored checks alone" in text for text in manifest["limitations"])
    assert report["outcome"] == report["task"]["lifecycle"] == "partial"


def test_passing_baseline_regression_blocks_verified_even_when_another_criterion_passes(
    app_env: AppEnvironment,
    tmp_path: Path,
) -> None:
    fixture = create_repository_fixture(tmp_path / "source")
    assert fixture.baseline_commit is not None
    repair_repository_fixture(fixture)
    before = (fixture.path / "calculator.py").read_text(encoding="utf-8")
    after = before.replace(REPAIRED_LINE, "    return sum(values) + offset - (not values)\n", 1)
    assert before != after
    patch = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="a/calculator.py",
            tofile="b/calculator.py",
        )
    ).encode("utf-8")
    contract = replace(
        fixture_contract(fixture.baseline_commit),
        objective="Preserve both established calculator behaviors while evaluating the patch.",
    )
    artifacts = ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root))
    pipeline = _controller(app_env, tmp_path, artifacts)
    created = pipeline.create(
        app_env.context,
        app_env.repository_id,
        fixture.path,
        contract,
        fixture_profile(),
        patch,
        "criterion-regression",
    )
    report = pipeline.resume(app_env.context, UUID(created["id"]))
    manifest = report["export"]["manifest"]
    checks = {check["check_id"]: check for check in manifest["checks"]}
    assert checks["total"]["baseline_status"] == checks["total"]["candidate_status"] == "passed"
    assert checks["total"]["comparison"] == "still_passed"
    assert checks["empty"]["baseline_status"] == "passed"
    assert checks["empty"]["candidate_status"] == checks["empty"]["status"] == "failed"
    assert checks["empty"]["comparison"] == "regression"
    assert {criterion["id"]: criterion["status"] for criterion in manifest["criteria"]} == {
        "R1": "passed",
        "R2": "failed",
    }
    assert any("empty: failed (regression)" in text for text in manifest["limitations"])
    assert report["outcome"] == report["task"]["lifecycle"] == "partial"
    assert (fixture.path / "calculator.py").read_text(encoding="utf-8") == before
