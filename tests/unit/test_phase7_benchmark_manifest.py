"""Held-out suite invariants; no model calls or fabricated success receipts."""

from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.evaluation.benchmark_ledger import BenchmarkSample, Outcome, SampleKind
from potbot.evaluation.benchmark_manifest import (
    BenchmarkCase,
    BenchmarkSuite,
    check_suite_samples,
)

D = "b" * 64
E = "c" * 64


def fixture():
    cases = tuple(BenchmarkCase(x, D, E, D, E, D) for x in ("case-a", "case-b"))
    suite = BenchmarkSuite(
        suite_id="held-out-v1",
        kind=SampleKind.LIVE_DEPLOYMENT,
        cases=cases,
        cohort_limits=(("single", 1), ("portfolio", 2)),
        model_ids=("provider:model-v1",),
        randomization_digest=E,
        max_money_microunits=1000,
    )
    samples = []
    for case in cases:
        for cohort in ("single", "portfolio"):
            samples.append(
                BenchmarkSample(
                    attempt_id=uuid4(),
                    case_id=case.case_id,
                    cohort=cohort,
                    ordinal=1,
                    model_id="provider:model-v1",
                    model_revision="snapshot1",
                    task_revision_digest=case.task_revision_digest,
                    repository_state_digest=case.repository_state_digest,
                    test_suite_digest=case.test_suite_digest,
                    receipt_digest=D,
                    status=Outcome.FAILED,
                    kind=SampleKind.LIVE_DEPLOYMENT,
                    duration_ms=50,
                    money_microunits=10,
                )
            )
    return suite, tuple(samples)


def test_suite_is_immutable_serializable_and_checkable():
    suite, attempts = fixture()
    assert BenchmarkSuite.from_dict(suite.to_dict()) == suite
    assert check_suite_samples(suite, attempts) == suite.digest
    assert suite.digest == BenchmarkSuite.from_dict(suite.to_dict()).digest
    assert "hidden_evaluator_digest" in suite.to_dict()["cases"][0]


def test_mismatched_task_or_model_cannot_enter_suite():
    suite, attempts = fixture()
    bad = replace(attempts[0], task_revision_digest="a" * 64)
    with pytest.raises(StateMismatchError):
        check_suite_samples(suite, (bad, *attempts[1:]))
    bad = replace(attempts[0], model_id="model-not-qualified")
    with pytest.raises(StateMismatchError):
        check_suite_samples(suite, (bad, *attempts[1:]))
    bad = replace(attempts[0], kind=SampleKind.SYNTHETIC_FIXTURE)
    with pytest.raises(StateMismatchError):
        check_suite_samples(suite, (bad, *attempts[1:]))


def test_missing_pairs_duplicate_ordinals_and_allowance_fail():
    suite, attempts = fixture()
    with pytest.raises(StateMismatchError):
        check_suite_samples(suite, attempts[:-1])
    with pytest.raises(StateMismatchError):
        check_suite_samples(suite, (*attempts, replace(attempts[0], attempt_id=uuid4())))
    with pytest.raises(StateMismatchError):
        check_suite_samples(
            suite,
            (
                *attempts,
                replace(
                    attempts[1],
                    attempt_id=uuid4(),
                    ordinal=3,
                ),
            ),
        )


def test_cost_ceiling_and_unknown_usage_are_explicit():
    suite, attempts = fixture()
    assert (
        check_suite_samples(
            suite,
            (
                *attempts[:-1],
                replace(
                    attempts[-1],
                    status=Outcome.INDETERMINATE,
                    receipt_digest=None,
                    money_microunits=None,
                ),
            ),
        )
        == suite.digest
    )
    with pytest.raises(StateMismatchError):
        check_suite_samples(replace(suite, max_money_microunits=20), attempts)


def test_boolean_limits_extra_fields_and_bad_hash_rejected():
    suite, _ = fixture()
    with pytest.raises(InvalidInputError):
        replace(suite, cohort_limits=(("single", True),))
    with pytest.raises(InvalidInputError):
        BenchmarkSuite.from_dict({**suite.to_dict(), "untrusted": "claim"})
    with pytest.raises(InvalidInputError):
        replace(suite, randomization_digest="not-a-digest")
    with pytest.raises(InvalidInputError):
        replace(suite.cases[0], hidden_evaluator_digest="unverified")


def test_readonly_benchmark_cli_checks_the_suite_but_withholds_scores(tmp_path, monkeypatch):
    import json
    import sys
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_phase7.py"
    spec = spec_from_file_location("benchmark_phase7_cli", script)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    suite, attempts = fixture()
    suite_file = tmp_path / "suite.json"
    samples_file = tmp_path / "samples.json"
    output_file = tmp_path / "audit.json"
    suite_file.write_text(json.dumps(suite.to_dict()), encoding="utf-8")
    samples_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "samples": [item.to_dict() for item in attempts],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(script),
            "--suite",
            str(suite_file),
            "--input",
            str(samples_file),
            "--output",
            str(output_file),
        ],
    )
    assert module.main() == 0
    report = json.loads(output_file.read_text(encoding="utf-8"))
    assert report["suite_digest"] == suite.digest
    assert report["suite_manifest_checked"] is True
    assert report["qualification"] == "unverified_claims"
    assert report["cohorts"]["single"]["verified_success_rate"] is None
