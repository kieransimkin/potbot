"""Deterministic model/role benchmarking, judge bias and monitoring safeguards."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.evaluation.agentic_model_bench import (
    Generation,
    ModelTarget,
    Observation,
    RoleCase,
    RoleSuite,
    judge_agreement,
    judge_case,
    monitor_history,
    observe_case,
    prompt_for_case,
    rubric_score,
    summarise,
)
from potbot.evaluation.agentic_model_transport import validate_loopback_url
from potbot.evaluation.agentic_monitor import HealthEvent, review_health_events


def target(name: str = "small", roles: tuple[str, ...] = ("triage", "judge")) -> ModelTarget:
    return ModelTarget(name, "qwen3:0.6b", "loopback_openai", roles, "rev-1",
                       base_url="http://127.0.0.1:11434/v1")


def case() -> RoleCase:
    return RoleCase("triage", "triage", "Return a JSON classification", "json_subset",
                    {"route": "local"})


def test_loopback_address_only_and_no_credentials() -> None:
    for bad in (
        "https://127.0.0.1:11434/v1", "http://example.com:11434/v1",
        "http://169.254.169.254:80/v1", "http://127.0.0.1:11434/admin",
        "http://user:password@localhost:11434/v1", "http://localhost/v1",
    ):
        with pytest.raises(InvalidInputError):
            validate_loopback_url(bad)
    assert validate_loopback_url("http://localhost:1234/v1")
    assert target().backend == "loopback_openai"


def test_suite_identity_and_no_hidden_answer_leak() -> None:
    suite = RoleSuite("tiny", (case(),))
    prompt = prompt_for_case(target().model, case(), "generic")
    assert "local" not in prompt.user
    assert "local" not in prompt.system.lower().split("benchmark task")[-1]
    assert suite.identity and suite.identity == RoleSuite("tiny", (case(),)).identity
    assert prompt.identity != prompt_for_case(target().model, case(), "model_role").identity


def test_rubrics_reject_wrong_claims_and_bool_for_int() -> None:
    assert rubric_score(case(), '{"route":"local", "other":"allowed"}') == 1
    assert rubric_score(case(), '{"route":"hosted"}') == 0
    assert rubric_score(case(), "not json") == 0
    numbers = RoleCase("numbers", "triage", "Return a number", "json_subset", {"count": 1})
    assert rubric_score(numbers, '{"count":true}') == 0
    assert rubric_score(numbers, '{"count":1}') == 1


def test_observation_records_hashes_and_no_answer_by_default() -> None:
    row = observe_case(model=target(), case=case(), variant="generic", trial=1,
                       generate=lambda _m, _p: Generation('{"route":"local"}', 12, 30, 5))
    assert row.score == 1
    assert row.output_sha256 and len(row.output_sha256) == 64
    assert row.response is None
    assert row.billed_microunits is None
    broken = observe_case(model=target(), case=case(), variant="generic", trial=1,
                          generate=lambda _m, _p: (_ for _ in ()).throw(RuntimeError("secret")))
    assert broken.status == "indeterminate" and broken.score is None
    assert "secret" not in json.dumps(broken.to_dict())


def test_no_routing_from_small_or_uncertain_samples() -> None:
    one = observe_case(model=target(), case=case(), variant="generic", trial=1,
                       generate=lambda _m, _p: Generation('{"route":"local"}', 10))
    assert summarise([one])["recommendations"]["triage"]["decision"] == "insufficient_evidence"
    incomplete = replace(one, status="indeterminate", score=None)
    many = [replace(one, trial=i) for i in range(12)] + [incomplete]
    assert summarise(many)["recommendations"]["triage"]["decision"] == "insufficient_evidence"
    unpaired_repeat = summarise([replace(one, trial=i) for i in range(12)])
    assert unpaired_repeat["recommendations"]["triage"]["decision"] == "insufficient_evidence"
    # Four different cases, each repeated three times, are eligible only for
    # advisory review. Repeated copies of one case never earn confidence.
    diverse = [replace(one, case_id=f"case-{n}", trial=i)
               for n in range(4) for i in range(3)]
    recommended = summarise(diverse)
    assert recommended["recommendations"]["triage"]["automatically_promoted"] is False
    assert recommended["production_qualified"] is False


def test_judge_order_flip_and_symmetric_accuracy() -> None:
    forward = judge_case("example", "2+2", "4", "5", reference_winner="A")
    reverse = judge_case("example", "2+2", "4", "5", reference_winner="A", reverse=True)
    assert forward.expected == {"choice": "A"}
    assert reverse.expected == {"choice": "B"}
    winner = lambda _m, p: Generation('{"choice":"B"}' if "Candidate A: 5" in p.user else '{"choice":"A"}', 2)
    rows = [observe_case(model=target(), case=c, variant="generic", trial=1,
                         generate=winner) for c in (forward, reverse)]
    assert all(r.score == 1 for r in rows)
    assert judge_agreement(rows)["pairs"][0]["symmetric_reference_accuracy"] == 1
    # A judge that always selects A fails the reverse presentation.
    biased = [observe_case(model=target(), case=c, variant="generic", trial=1,
                           generate=lambda _m, _p: Generation('{"choice":"A"}', 2))
              for c in (forward, reverse)]
    assert judge_agreement(biased)["pairs"][0]["symmetric_reference_accuracy"] == 0


def test_history_compares_matching_suite_and_never_autopromotes() -> None:
    row = {"role":"triage", "model_id":"tiny", "variant":"generic", "completed":12,
           "indeterminate":0, "accuracy":1.0}
    reports = [
        {"suite_sha256":"a"*64,"run_id":"old","summary":{"rows":[row]}},
        {"suite_sha256":"a"*64,"run_id":"new","summary":{"rows":[{**row,"accuracy":0.7}]}},
    ]
    result = monitor_history(reports)
    assert result["automatic_changes"] == 0
    assert result["alerts"][0]["decision"] == "investigate_regression"
    reports[1]["suite_sha256"] = "b" * 64
    assert monitor_history(reports)["alerts"] == []


def test_workflow_assumption_monitor_requires_evidence_and_repeated_events() -> None:
    raw = {"kind":"assumption","identity":"source-demand","case_id":"case1",
           "status":"passed","source_digest":"a"*64,"observed_at":"2026-10-20T12:00:00Z"}
    records = [HealthEvent.from_dict({**raw,"observed_at":f"2026-10-{i:02}T12:00:00Z",
                                     "status":"passed" if i <= 12 else "failed"})
               for i in range(1,25)]
    result = review_health_events(records)
    assert result["alerts"][0]["condition"] == "quality_regression"
    assert result["automatic_workflow_changes"] == 0
    assert result["snapshots"][0]["receipt_qualified"] is False
    with pytest.raises(InvalidInputError):
        HealthEvent.from_dict({**raw,"source_digest":"short"})


def test_bundled_suite_is_typed_and_includes_graph_roles() -> None:
    source = Path(__file__).resolve().parents[2] / "examples/agentic-benchmark/role-suite.json"
    suite = RoleSuite.from_dict(json.loads(source.read_text()))
    assert {"triage","planning","evidence","coding","steering","critique","judge"} <= {c.role for c in suite.cases}
    assert all(c.split == "holdout" for c in suite.cases)


def test_structural_hypothesis_fixture_is_not_evidence_of_correctness() -> None:
    role_case = RoleCase(
        "hypothesis", "hypothesis", "Propose an investigable claim.", "json_shape",
        {"hypotheses": [{"hypothesis": "text", "assumptions": "list", "questions": "list"}]},
    )
    assert rubric_score(role_case, json.dumps({"hypotheses": [
        {"hypothesis": "Could be wrong", "assumptions": [], "questions": []}
    ]})) == 1
    assert rubric_score(role_case, json.dumps({"hypotheses": [{"hypothesis": 5}]})) == 0


def test_routing_requires_paired_tasks_across_models() -> None:
    first = observe_case(model=target(), case=case(), variant="generic", trial=1,
                         generate=lambda _m, _p: Generation('{"route":"local"}', 10))
    other = replace(first, model_id="other-model", tier="micro")
    rows = [replace(first, trial=i, case_id=f"c{i}") for i in range(12)]
    rows += [replace(other, trial=i, case_id=f"different{i}") for i in range(12)]
    report = summarise(rows)
    assert report["recommendations"]["triage"]["decision"] == "unpaired_evidence"


def test_monitor_keeps_model_revisions_and_task_cohorts_separate() -> None:
    baseline = {"role": "coding", "model_id": "small", "variant": "family",
                "completed": 12, "indeterminate": 0, "accuracy": 1.0,
                "revision": "sha-1", "case_trials": [["a", i] for i in range(12)]}
    history = [
        {"suite_sha256": "f" * 64, "run_id": "r1", "summary": {"rows": [baseline]}},
        {"suite_sha256": "f" * 64, "run_id": "r2", "summary": {"rows": [{**baseline,
            "revision": "sha-2", "accuracy": 0.5}]}},
        {"suite_sha256": "f" * 64, "run_id": "r3", "summary": {"rows": [{**baseline,
            "case_trials": [["b", i] for i in range(12)], "accuracy": 0.5}]}},
    ]
    assert monitor_history(history)["alerts"] == []


def test_health_monitor_rejects_invalid_timestamp_types_without_crashing() -> None:
    raw = {"kind": "workflow", "identity": "keywordmoves", "case_id": "stage",
           "status": "passed", "source_digest": "a" * 64, "observed_at": 20261025}
    with pytest.raises(InvalidInputError):
        HealthEvent.from_dict(raw)
