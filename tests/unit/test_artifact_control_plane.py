"""Offline evidence-validation tests; these do not qualify PostgreSQL or workers."""

from __future__ import annotations

import ast
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.evaluation.artifact_control_plane import (
    check_control_plane_report,
    control_plane_summary,
)

OPEN = "syn://cog/fixture-open"
READ = "syn://cog/fixture-read"
REQUIRED = {OPEN, READ}
ROOT = Path(__file__).resolve().parents[2]


def lifecycle(ref=OPEN, *, offset=0, terminal="completed", denied=False):
    run = str(uuid4())
    metadata = {
        "cog_ref": ref,
        "child_runs": [],
        "binding_hash": "a" * 64,
        "semantic_snapshot_hash": "b" * 64,
        "task_revision_id": str(uuid4()),
        "repository_state_id": str(uuid4()),
        "execution_scope_hash": "c" * 64,
        "policy_hash": "d" * 64,
        "metrics": {
            "controller_cpu_ms": {"value": 1, "unit": "ms", "provenance": "measured"},
            "elapsed_wall_ms": {"value": 2, "unit": "ms", "provenance": "measured"},
        },
    }
    stages = ["failed"] if denied else ["started", "updated", terminal]
    rows = []
    for i, stage in enumerate(stages):
        rows.append({
            "id": str(uuid4()), "sequence": offset + i + 1, "run_id": run,
            "parent_run_id": run if rows else None,
            "parent_event_id": rows[-1]["id"] if rows else None,
            "stage": stage,
            "telemetry": {**copy.deepcopy(metadata), "phase": "admission_denied" if denied else "test"},
            "output_artifact_ref": "artifact:" + str(uuid4()) if stage == "completed" else None,
            "output_sha256": "e" * 64 if stage == "completed" else None,
        })
    return rows


def ledger():
    return lifecycle(OPEN) + lifecycle(READ, offset=3)


def test_complete_controller_roots_do_not_claim_worker_qualification():
    events = ledger()
    summary = control_plane_summary(events, required_cogs=REQUIRED)
    assert summary["scope"] == "registered_artifact_controller_cogs"
    assert summary["sandbox_worker_qualified"] is False
    assert summary["root_invocations"] == 2
    assert summary["terminal_counts"]["completed"] == 2
    assert check_control_plane_report(summary, events, required_cogs=REQUIRED) == summary


def test_concurrent_interleaving_and_gaps_in_workspace_cursor_are_valid():
    left, right = lifecycle(OPEN), lifecycle(READ)
    events = [entry for pair in zip(left, right, strict=True) for entry in pair]
    for i, event in enumerate(events):
        event["sequence"] = 10 + i * 3  # Other event streams can share the workspace cursor.
    assert control_plane_summary(events, required_cogs=REQUIRED)["root_invocations"] == 2


@pytest.mark.parametrize("terminal", ["failed", "indeterminate", "cancelled"])
def test_negative_runs_do_not_count_as_success(terminal):
    events = ledger() + lifecycle(READ, offset=6, terminal=terminal)
    summary = control_plane_summary(events, required_cogs=REQUIRED)
    assert summary["terminal_counts"][terminal] == 1
    assert summary["terminal_counts"]["completed"] == 2


def test_single_admission_denial_is_retained_but_not_successful_coverage():
    events = ledger() + lifecycle(READ, offset=6, denied=True)
    assert control_plane_summary(events, required_cogs=REQUIRED)["admission_denials"] == 1
    with pytest.raises(ValueError, match="successful required Cog"):
        control_plane_summary(lifecycle(OPEN) + lifecycle(READ, offset=3, denied=True),
                              required_cogs=REQUIRED)


def test_readback_cogs_can_be_allowed_without_demanding_every_optional_cog():
    events = ledger()
    summary = control_plane_summary(events, required_cogs={OPEN}, allowed_cogs=REQUIRED)
    assert summary["completed_cogs"] == sorted(REQUIRED)


def mutate(events, case):
    last = events[2]
    if case == "empty":
        events.clear()
    elif case == "duplicate-event":
        events[1]["id"] = events[0]["id"]
    elif case == "sequence-reversal":
        events[2]["sequence"] = events[1]["sequence"]
    elif case == "boolean-sequence":
        events[0]["sequence"] = True
    elif case == "bad-uuid":
        events[0]["run_id"] = "not-a-uuid"
    elif case == "missing-parent-field":
        events[1].pop("parent_run_id")
    elif case == "cross-run-parent":
        events[1]["parent_run_id"] = events[3]["run_id"]
    elif case == "broken-event-chain":
        last["parent_event_id"] = events[0]["id"]
    elif case == "external-root":
        events[0]["parent_run_id"] = str(uuid4())
    elif case == "external-root-event":
        events[0]["parent_event_id"] = str(uuid4())
    elif case == "unfinished":
        last["stage"] = "updated"
    elif case == "no-progress":
        del events[1]
    elif case == "early-terminal":
        events[1]["stage"] = "completed"
    elif case == "repeated-start":
        events[1]["stage"] = "started"
    elif case == "wrong-operation":
        last["telemetry"]["cog_ref"] = "syn://cog/arbitrary-worker"
    elif case == "nested-child":
        last["telemetry"]["child_runs"] = [{"run_id": str(uuid4())}]
    elif case == "changed-policy":
        last["telemetry"]["policy_hash"] = "f" * 64
    elif case == "changed-task":
        last["telemetry"]["task_revision_id"] = str(uuid4())
    elif case == "missing-pinned-scope":
        last["telemetry"].pop("execution_scope_hash")
    elif case == "missing-output":
        last["output_artifact_ref"] = None
    elif case == "invalid-output-sha":
        last["output_sha256"] = "bad"
    elif case == "unmeasured":
        last["telemetry"]["metrics"]["controller_cpu_ms"]["provenance"] = "estimated"
    elif case == "negative-metric":
        last["telemetry"]["metrics"]["elapsed_wall_ms"]["value"] = -1
    elif case == "boolean-metric":
        last["telemetry"]["metrics"]["elapsed_wall_ms"]["value"] = True
    elif case == "nan-metric":
        last["telemetry"]["metrics"]["elapsed_wall_ms"]["value"] = float("nan")
    elif case == "infinite-metric":
        last["telemetry"]["metrics"]["elapsed_wall_ms"]["value"] = float("inf")
    elif case == "huge-metric":
        last["telemetry"]["metrics"]["elapsed_wall_ms"]["value"] = 10**1000
    elif case == "missing-metric":
        last["telemetry"]["metrics"].pop("controller_cpu_ms")
    else:
        raise AssertionError(case)


@pytest.mark.parametrize("case", [
    "empty", "duplicate-event", "sequence-reversal", "boolean-sequence", "bad-uuid",
    "missing-parent-field", "cross-run-parent", "broken-event-chain", "external-root",
    "external-root-event", "unfinished", "no-progress", "early-terminal", "repeated-start",
    "wrong-operation", "nested-child", "changed-policy", "changed-task", "missing-pinned-scope",
    "missing-output", "invalid-output-sha", "unmeasured", "negative-metric", "boolean-metric",
    "nan-metric", "infinite-metric", "huge-metric", "missing-metric",
])
def test_invalid_evidence_is_rejected(case):
    events = ledger()
    mutate(events, case)
    with pytest.raises(ValueError, match="Artifact control-plane evidence"):
        control_plane_summary(events, required_cogs=REQUIRED)


def test_orphan_failure_does_not_masquerade_as_admission_denial():
    events = ledger() + lifecycle(READ, offset=6, denied=True)
    events[-1]["telemetry"]["phase"] = "result_received"
    with pytest.raises(ValueError, match="orphan failure"):
        control_plane_summary(events, required_cogs=REQUIRED)


@pytest.mark.parametrize("change", ["worker", "integer-bool", "extra-field", "missing", "count"])
def test_scope_report_must_match_exactly(change):
    events = ledger()
    summary = control_plane_summary(events, required_cogs=REQUIRED)
    if change == "worker":
        summary["sandbox_worker_qualified"] = True
    elif change == "integer-bool":
        summary["sandbox_worker_qualified"] = 0
    elif change == "extra-field":
        summary["sandbox_accepted"] = True
    elif change == "missing":
        summary = None
    else:
        summary["root_invocations"] += 1
    with pytest.raises(ValueError, match="scope report"):
        check_control_plane_report(summary, events, required_cogs=REQUIRED)


def test_checks_survive_optimized_python():
    program = (
        "from potbot.evaluation.artifact_control_plane import control_plane_summary; "
        "control_plane_summary([], required_cogs={'required'})"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    run = subprocess.run([sys.executable, "-O", "-c", program], env=env,
                         capture_output=True, text=True, timeout=15, check=False)
    assert run.returncode != 0
    assert "empty or oversized ledger" in run.stderr


@pytest.mark.parametrize("singular,plural", [("range", "ranges"), ("upload", "uploads")])
def test_acceptance_fixtures_use_the_registered_controller_boundary(singular, plural):
    fixture = ROOT / f"tests/integration/test_artifact_{singular}_cogs.py"
    gate = ROOT / f"scripts/check_artifact_{plural}.py"
    tree, gate_tree = ast.parse(fixture.read_text()), ast.parse(gate.read_text())
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not {"HostOperation", "WORK_COGS", "WORK_KINDS"} & names
    runtimes = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "ExecutionCogRuntime"]
    assert runtimes
    for call in runtimes:
        operations = next(kw.value for kw in call.keywords if kw.arg == "operations")
        assert isinstance(operations, ast.Tuple) and operations.elts == []
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and isinstance(node.func.value, ast.Name) and node.func.value.id == "registry"
               and node.func.attr == "invoke" for node in ast.walk(tree))
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == "check_control_plane_report" for node in ast.walk(gate_tree))

    def source_list(parsed):
        return next(ast.literal_eval(node.value) for node in parsed.body
                    if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "SOURCES" for t in node.targets))

    sources = source_list(tree)
    assert sources == source_list(gate_tree)
    assert str(fixture.relative_to(ROOT)) in sources
    assert str(gate.relative_to(ROOT)) in sources
    assert "src/potbot/tools/sandbox_cog_worker.py" in sources
    assert "src/potbot/evaluation/artifact_control_plane.py" in sources
    # JSON round-trip preserves the boolean qualification limit.
    assert json.loads(json.dumps(control_plane_summary(ledger(), required_cogs=REQUIRED)))[
        "sandbox_worker_qualified"
    ] is False
