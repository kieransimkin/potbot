"""A green subset, skipped database suite or stale source cannot certify a programme."""

import json
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.evaluation.tool_programme import FEATURE_GATES, REQUIRED_CHECKS, evaluate, junit_counts

SOURCE = {"commit": "a" * 40, "tree": "b" * 40, "clean": True}
ROOT = Path(__file__).resolve().parents[2]
STAGES = json.loads(
    (
        ROOT / "docs/potbot-tool-mcp-implementation-bundle/potbot-tool-integration-stages.json"
    ).read_text()
)


@pytest.fixture
def evidence(tmp_path):
    summary = {
        "source": SOURCE,
        "source_at_start": SOURCE,
        "success": True,
        "postgres_requested": True,
        "sandbox_requested": True,
        "checks": [{"check": c, "exit_code": 0} for c in REQUIRED_CHECKS],
    }
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    for name in ("unit-and-git", "postgres", "sandbox"):
        (tmp_path / f"{name}.xml").write_text(
            '<testsuites><testsuite><testcase name="actual"/></testsuite></testsuites>'
        )
    for _, relative in FEATURE_GATES.values():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"result": "passed", "source_commit": SOURCE["commit"], "version": "test"})
        )
    return tmp_path


def gate(evidence, **kwargs):
    return evaluate(stages=STAGES, artifacts=evidence, source=SOURCE, version="test", **kwargs)


def test_fixture_success_reports_every_stage_without_promoting_subsets(evidence):
    result = gate(evidence)
    assert result["result"] == "passed" and result["implemented_profile_passed"]
    assert result["full_programme_qualified"] is False
    assert len(result["stages"]) == 48
    assert [r["stage"] for r in result["stages"] if r["result"] == "passed"] == ["T01.1", "T01.2"]
    assert next(r for r in result["stages"] if r["stage"] == "T01.3")["result"] == "incomplete"
    assert gate(evidence, mode="full_programme")["result"] == "failed"


@pytest.mark.parametrize("name", ["postgres", "sandbox"])
@pytest.mark.parametrize(
    "body", ["", '<skipped message="missing host"/>', "<error/>", "<failure/>"]
)
def test_green_exit_does_not_override_empty_failed_or_skipped_mandatory_suite(evidence, name, body):
    case = f'<testcase name="required">{body}</testcase>' if body else ""
    (evidence / f"{name}.xml").write_text(
        f'<testsuites tests="9999" failures="0">{case}</testsuites>'
    )
    assert gate(evidence)["result"] == "failed"


@pytest.mark.parametrize("mutation", ["stale", "missing", "failed", "version"])
def test_enabled_feature_is_mandatory_and_exact_source_bound(evidence, mutation):
    path = evidence / FEATURE_GATES["artifact_uploads"][1]
    row = json.loads(path.read_text())
    if mutation == "missing":
        path.unlink()
    else:
        row[{"stale": "source_commit", "failed": "result", "version": "version"}[mutation]] = (
            "wrong"
        )
        path.write_text(json.dumps(row))
    assert gate(evidence)["result"] == "failed"
    disabled = gate(
        evidence, enabled_features=tuple(f for f in FEATURE_GATES if f != "artifact_uploads")
    )
    assert disabled["result"] == "passed"
    assert (
        next(f for f in disabled["features"] if f["feature"] == "artifact_uploads")["result"]
        == "disabled"
    )


def test_source_changed_during_run_and_duplicate_required_check_fail(evidence):
    path = evidence / "summary.json"
    row = json.loads(path.read_text())
    row["source_at_start"] = {**SOURCE, "commit": "c" * 40}
    row["checks"].append({"check": "postgres", "exit_code": 0})
    path.write_text(json.dumps(row))
    assert gate(evidence)["result"] == "failed"
    with pytest.raises(InvalidInputError):
        evaluate(
            stages=STAGES, artifacts=evidence, source={**SOURCE, "clean": False}, version="test"
        )


def test_junit_external_entities_are_rejected(evidence):
    path = evidence / "unsafe.xml"
    path.write_text('<!DOCTYPE data [<!ENTITY a SYSTEM "file:///private">]><testsuites/>')
    with pytest.raises(InvalidInputError):
        junit_counts(path)
