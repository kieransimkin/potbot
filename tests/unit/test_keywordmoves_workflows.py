"""Guarded KeywordMoves workflow composition: no third-party dependency required."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from potbot.controller.keywordmoves_workflows import (
    KeywordMovesWorkflow,
    WorkflowExecutionPolicy,
    WorkflowPlan,
    WorkflowStep,
)
from potbot.domain.errors import InvalidInputError, StateMismatchError

REV = "9044ba40460af6e6165f6b2c49a0d2454704fdd7"


def _stub(*, plugin, operation, inputs, keywords, options):
    return {"plugin": plugin, "operation": operation,
            "keywords": [{"phrase": "coastal memories", "relationship": "proposal",
                          "score": None, "evidence": [], "metadata": {}}],
            "metadata": {"provider": "fixture", "external_demand": "unvalidated"}, "notes": []}


def _plan(*steps: WorkflowStep) -> WorkflowPlan:
    return WorkflowPlan(REV, "Example Song", steps)


def test_stage_dag_executes_in_order_and_exposes_receipted_r4_evidence(tmp_path: Path):
    source = tmp_path / "input"
    source.mkdir()
    (source / "lyrics.txt").write_text("Coastal memories", encoding="utf-8")
    executed = []
    events = []

    def fake(**kwargs):
        executed.append((kwargs["plugin"], kwargs["operation"], kwargs["inputs"]))
        return _stub(**kwargs)

    plan = _plan(
        WorkflowStep("extract", "text-library", "extract-literal", ("file:lyrics.txt",)),
        WorkflowStep("focus", "candidate-focus", "related", ("step:extract",),
                     ("coastal memories",)),
        WorkflowStep("pool", "@potbot", "bind-target", ("step:focus",),
                     options=(("subject", "Example Song"), ("platform", "YouTube"))),
    )
    flow = KeywordMovesWorkflow(plan=plan, input_root=source, output_root=tmp_path / "out",
                                invoke=fake, emit=lambda event, data: events.append((event, data)))
    result = flow.run()
    assert result.status == "completed"
    assert result.completed == ("extract", "focus", "pool")
    assert len(executed) == 2  # built-in stage never invokes an external adapter
    assert executed[1][2] == (tmp_path / "out" / "results" / "extract.json",)
    pool = json.loads((tmp_path / "out/results/pool.json").read_text())
    assert pool["metadata"]["target_subject"] == "Example Song"
    assert pool["metadata"]["target_platform"] == "YouTube"
    assert pool["metadata"]["potbot_source_sha256"] == result.artifacts["focus"]
    assert len([e for e in events if e[0] == "keywordmoves.step.completed"]) == 3
    evidence = flow.evidence("focus", run_id="a" * 32)
    from potbot.controller.reasoning_investigation import EvidenceGateway

    gateway = EvidenceGateway((flow.as_evidence_tool(("focus", "pool")),))
    findings = gateway.search(channel="internal_workflows", query="coastal memories",
                              run_id="a" * 32)
    assert findings and findings[0].source_sha256 == result.artifacts["focus"]
    assert gateway.search(channel="internal_workflows", query="unrelated tag",
                          run_id="a" * 32) == ()
    assert evidence.source_sha256 == result.artifacts["focus"]
    assert evidence.receipt_ref.startswith("sha256:")
    assert "no cross-platform search demand" in evidence.excerpt
    with pytest.raises(StateMismatchError):
        flow.run()  # no automatic replay
    assert flow.run(resume=True).artifacts == result.artifacts
    assert len(executed) == 2  # completed operations were not called again


def test_missing_editorial_review_pauses_and_resume_executes_only_new_stage(tmp_path: Path):
    source = tmp_path / "input"
    source.mkdir()
    (source / "lyrics.txt").write_text("Coastal memories", encoding="utf-8")
    calls = []

    def fake(**kwargs):
        calls.append(kwargs["operation"])
        return _stub(**kwargs)

    plan = _plan(
        WorkflowStep("extract", "text-library", "extract-local", ("file:lyrics.txt",)),
        WorkflowStep("shortlist", "candidate-shortlist", "select",
                     ("file:review.json", "step:extract")),
    )
    flow = KeywordMovesWorkflow(plan=plan, input_root=source, output_root=tmp_path / "out",
                                invoke=fake)
    first = flow.run()
    assert first.status == "awaiting_input"
    assert first.waiting_for == "file:review.json"
    assert calls == ["extract-local"]
    (source / "review.json").write_text("{}", encoding="utf-8")
    second = flow.run(resume=True)
    assert second.completed == ("extract", "shortlist")
    assert calls == ["extract-local", "select"]


def test_source_mutation_invalidates_resume_before_replay(tmp_path: Path):
    source = tmp_path / "input"
    source.mkdir()
    file = source / "lyrics.txt"
    file.write_text("original", encoding="utf-8")
    plan = _plan(WorkflowStep("extract", "text-library", "extract-local",
                              ("file:lyrics.txt",)))
    flow = KeywordMovesWorkflow(plan=plan, input_root=source,
                                output_root=tmp_path / "out", invoke=_stub)
    flow.run()
    file.write_text("changed", encoding="utf-8")
    with pytest.raises(StateMismatchError, match="source changed"):
        flow.run(resume=True)


def test_uncertain_stage_not_replayed(tmp_path: Path):
    source = tmp_path / "input"
    source.mkdir()
    attempts = []

    def failed(**kwargs):
        attempts.append(kwargs["operation"])
        raise OSError("transient source failure")

    flow = KeywordMovesWorkflow(
        plan=_plan(WorkflowStep("profiles", "hashtag-planner", "profiles")),
        input_root=source, output_root=tmp_path / "out", invoke=failed)
    with pytest.raises(OSError):
        flow.run()
    with pytest.raises(StateMismatchError, match="uncertain"):
        flow.run(resume=True)
    assert attempts == ["profiles"]


def test_remote_paid_and_unknown_operations_cannot_be_granted_by_manifest(tmp_path: Path):
    source = tmp_path / "input"
    source.mkdir()
    remote = WorkflowStep("remote", "google-trends", "collect-browser")
    with pytest.raises(InvalidInputError, match="not enabled"):
        KeywordMovesWorkflow(plan=_plan(remote), input_root=source,
                             output_root=tmp_path / "out", invoke=_stub)
    policy = WorkflowExecutionPolicy(
        allowed_operations=frozenset({("google-trends", "collect-browser")})
    )
    with pytest.raises(InvalidInputError, match="approval"):
        KeywordMovesWorkflow(plan=_plan(remote), input_root=source,
                             output_root=tmp_path / "out", invoke=_stub, policy=policy)
    approved = WorkflowStep("remote", "google-trends", "collect-browser",
                            approval="source-scope-accepted")
    trusted_policy = WorkflowExecutionPolicy(
        allowed_operations=frozenset({("google-trends", "collect-browser")}),
        approved_external=frozenset({"source-scope-accepted"}),
    )
    runner = KeywordMovesWorkflow(plan=_plan(approved), input_root=source,
                                   output_root=tmp_path / "out", invoke=_stub,
                                   policy=trusted_policy)
    assert runner.run().status == "completed"


def test_secret_options_paths_symlinks_cycles_and_oversize_fail(tmp_path: Path):
    with pytest.raises(InvalidInputError, match="credentials"):
        WorkflowStep("bad", "text-library", "extract-local",
                     options=(("api_key", "SECRET"),))
    with pytest.raises(InvalidInputError, match="missing or later"):
        _plan(WorkflowStep("first", "text-library", "extract-local",
                           ("step:second",)))
    with pytest.raises(InvalidInputError, match="select a file"):
        WorkflowStep("bad", "text-library", "extract-local", ("file:",))
    root = tmp_path / "inputs"
    root.mkdir()
    try:
        (root / "outside").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("filesystem does not allow test symlinks")
    flow = KeywordMovesWorkflow(
        plan=_plan(WorkflowStep("extract", "text-library", "extract-local",
                                ("file:outside/secret.txt",))),
        input_root=root, output_root=tmp_path / "results", invoke=_stub)
    with pytest.raises(InvalidInputError, match="symlink"):
        flow.run()
    (root / "large.txt").write_bytes(b"z" * 2_000_001)
    too_large = KeywordMovesWorkflow(
        plan=_plan(WorkflowStep("extract", "text-library", "extract-local",
                                ("file:large.txt",))),
        input_root=root, output_root=tmp_path / "another", invoke=_stub)
    with pytest.raises(InvalidInputError, match="bound"):
        too_large.run()


def test_single_writer_lock_and_input_race_are_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "inputs"
    source.mkdir()
    lyrics = source / "lyrics.txt"
    lyrics.write_text("coastal", encoding="utf-8")
    plan = _plan(WorkflowStep("extract", "text-library", "extract-local",
                              ("file:lyrics.txt",)))
    output = tmp_path / "output"
    calls = []

    def competing(**kwargs):
        calls.append(kwargs["operation"])
        second = KeywordMovesWorkflow(plan=plan, input_root=source,
                                      output_root=output, invoke=_stub)
        with pytest.raises(StateMismatchError, match="active or unreviewed writer"):
            second.run()
        lyrics.write_text("modified while running", encoding="utf-8")
        return _stub(**kwargs)

    flow = KeywordMovesWorkflow(plan=plan, input_root=source,
                                output_root=output, invoke=competing)
    with pytest.raises(StateMismatchError, match="changed during execution"):
        flow.run()
    assert calls == ["extract-local"]
    with pytest.raises(StateMismatchError, match="uncertain"):
        flow.run(resume=True)
    assert not (output / ".keywordmoves-writer.lock").exists()


def test_source_preserving_merge_keeps_incompatible_units_separate(tmp_path: Path) -> None:
    root = tmp_path / "inputs"
    root.mkdir()
    a = {"plugin": "google-trends", "operation": "import-interest", "metadata": {},
         "keywords": [{"phrase": "coastal memories", "score": 0.9,
                       "evidence": [{"source": "Google Trends", "metric": "relative_interest_mean",
                                     "value": 80, "unit": "index_0_100"}], "metadata": {}}]}
    b = {"plugin": "observed-evidence", "operation": "import-observations",
         "metadata": {}, "keywords": [{"phrase": "Coastal Memories", "score": None,
                                       "evidence": [{"source": "Reviewed export", "metric": "impressions",
                                                     "value": 12, "unit": "count"}], "metadata": {}}]}
    (root / "a.json").write_text(json.dumps(a), encoding="utf-8")
    (root / "b.json").write_text(json.dumps(b), encoding="utf-8")
    flow = KeywordMovesWorkflow(
        plan=_plan(WorkflowStep("merge", "@potbot", "merge-candidates",
                                ("file:a.json", "file:b.json"))),
        input_root=root, output_root=tmp_path / "output", invoke=_stub)
    result = flow.run()
    payload = json.loads((tmp_path / "output/results/merge.json").read_text())
    assert len(payload["keywords"]) == 1
    row = payload["keywords"][0]
    assert row["score"] is None
    assert [ev["unit"] for ev in row["evidence"]] == ["index_0_100", "count"]
    assert [r["original_score"] for r in row["metadata"]["source_lineage"]] == [0.9, None]
    assert len(result.artifacts) == 1


def test_manifest_schema_round_trip_and_catalogue_classification() -> None:
    plan = _plan(WorkflowStep("literal", "text-library", "extract-literal"))
    assert WorkflowPlan.from_dict(plan.to_dict()).sha256 == plan.sha256
    with pytest.raises(InvalidInputError, match="manifest schema"):
        WorkflowPlan.from_dict({"schema": "not-a-workflow", "steps": []})
    catalogue = {"text-library": ("extract-literal", "extract"),
                 "keybert": ("extract",), "google-trends": ("collect-browser",)}
    rows = WorkflowExecutionPolicy().inventory(catalogue)
    index = {(r["plugin"], r["operation"]): r["policy"] for r in rows}
    assert index[("text-library", "extract-literal")] == "offline_registered"
    assert index[("text-library", "extract")] == "not_enabled"
    assert index[("keybert", "extract")] == "not_enabled"  # may download model files
    assert index[("google-trends", "collect-browser")] == "not_enabled"


def test_real_keywordmoves_tuple_shaped_plugin_result_is_canonicalised(tmp_path: Path):
    source = tmp_path / "input"
    source.mkdir()
    (source / "lyrics.txt").write_text("Coastal memories", encoding="utf-8")

    def keywordmoves_style(**kwargs):
        return {
            "plugin": kwargs["plugin"], "operation": kwargs["operation"],
            "keywords": ({"phrase": "coastal memories", "evidence": (
                {"source": "text-library", "metric": "occurrences", "value": 1},
            ), "metadata": {}},),
            "notes": ("Local evidence, not external demand.",),
            "metadata": {},
        }

    flow = KeywordMovesWorkflow(
        plan=_plan(WorkflowStep("literal", "text-library", "extract-literal",
                                ("file:lyrics.txt",))),
        input_root=source, output_root=tmp_path / "out", invoke=keywordmoves_style,
    )
    result = flow.run()
    assert result.status == "completed"
    decoded = json.loads((tmp_path / "out/results/literal.json").read_text())
    assert isinstance(decoded["keywords"], list)
    assert isinstance(decoded["keywords"][0]["evidence"], list)
    assert isinstance(decoded["notes"], list)
