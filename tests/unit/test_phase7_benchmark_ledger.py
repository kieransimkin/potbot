"""Fair-cohort metric projection cannot turn unsigned claims into model scores."""

from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.evaluation.benchmark_ledger import (
    BenchmarkSample,
    Outcome,
    SampleKind,
    evaluate_benchmark,
)

D = "b" * 64


def sample(case, cohort, ordinal, status, *, kind=SampleKind.LIVE_DEPLOYMENT, price=10, receipt=D):
    return BenchmarkSample(
        attempt_id=uuid4(),
        case_id=case,
        cohort=cohort,
        ordinal=ordinal,
        model_id="provider:openai/model-x",
        model_revision="pinned-model-x",
        task_revision_digest=D,
        repository_state_digest=D,
        test_suite_digest=D,
        receipt_digest=receipt,
        status=status,
        kind=kind,
        duration_ms=100,
        money_microunits=price,
    )


class FixtureChecker:
    def qualified(self, item):
        return item.receipt_digest == D


def paired():
    return (
        sample("a", "single", 1, Outcome.FAILED),
        sample("b", "single", 1, Outcome.VERIFIED),
        sample("a", "portfolio", 1, Outcome.FAILED),
        sample("a", "portfolio", 2, Outcome.VERIFIED),
        sample("b", "portfolio", 1, Outcome.VERIFIED),
    )


def test_unchecked_provider_claims_are_not_benchmarks():
    result = evaluate_benchmark(paired())
    assert result["qualification"] == "unverified_claims"
    assert result["cohorts"]["single"]["verified_success_rate"] is None
    assert result["cohorts"]["portfolio"]["reported_successes_not_trusted"] == 2
    assert result["paired_comparison"] == {}


def test_exact_matched_cohorts_and_qualified_receipts():
    result = evaluate_benchmark(paired(), checker=FixtureChecker())
    assert result["qualification"] == "checked_external_receipts"
    assert result["cohorts"]["single"]["verified_success_rate"] == 0.5
    assert result["cohorts"]["portfolio"]["verified_success_rate"] == 1.0
    assert result["cohorts"]["portfolio"]["measured_money_microunits"] == 30
    assert result["paired_comparison"]["improved"] == 1
    assert result["paired_comparison"]["regressed"] == 0
    assert result["paired_comparison"]["success_difference"] == 0.5
    assert result["cohorts"]["single"]["wilson_95"][0] < 0.5


def test_mismatched_tests_or_incomplete_cohort_rejected():
    a = paired()
    with pytest.raises(StateMismatchError):
        evaluate_benchmark(a[:-1])
    with pytest.raises(StateMismatchError):
        evaluate_benchmark(
            a + (replace(a[0], attempt_id=uuid4(), test_suite_digest="c" * 64, cohort="third"),)
        )
    with pytest.raises(InvalidInputError):
        evaluate_benchmark(a + (a[0],))


def test_uncertain_or_untrusted_receipts_cannot_score_success():
    a = list(paired())
    a[3] = replace(a[3], status=Outcome.INDETERMINATE, receipt_digest=None)
    r = evaluate_benchmark(tuple(a), checker=FixtureChecker())
    assert r["cohorts"]["portfolio"]["verified_success_rate"] is None
    assert r["cohorts"]["portfolio"]["indeterminate_cases"] == 1
    a = list(paired())
    a[3] = replace(a[3], receipt_digest="c" * 64)
    r = evaluate_benchmark(tuple(a), checker=FixtureChecker())
    assert r["cohorts"]["portfolio"]["verified_success_rate"] is None


def test_missing_usage_and_synthetic_data_are_separated():
    a = tuple(
        replace(item, kind=SampleKind.SYNTHETIC_FIXTURE, money_microunits=None) for item in paired()
    )
    result = evaluate_benchmark(a, checker=FixtureChecker())
    assert result["classification"] == "synthetic_fixture"
    assert result["cohorts"]["single"]["money_unknown_attempts"] == 2
    assert result["cohorts"]["single"]["measured_money_microunits"] is None
    with pytest.raises(StateMismatchError):
        evaluate_benchmark(a + (paired()[0],))


def test_boolean_and_negative_measurements_rejected():
    with pytest.raises(InvalidInputError):
        replace(paired()[0], ordinal=True)
    with pytest.raises(InvalidInputError):
        replace(paired()[0], duration_ms=-1)
    with pytest.raises(InvalidInputError):
        replace(paired()[0], status=Outcome.VERIFIED, receipt_digest=None)


def test_sample_export_and_strict_import():
    original = paired()[0]
    assert BenchmarkSample.from_dict(original.to_dict()) == original
    unsafe = original.to_dict()
    unsafe["extra"] = "unreviewed"
    with pytest.raises(InvalidInputError):
        BenchmarkSample.from_dict(unsafe)
    changed = original.to_dict()
    changed["attempt_id"] = "INVALID"
    with pytest.raises(InvalidInputError):
        BenchmarkSample.from_dict(changed)


def test_uncertain_provider_costs_are_never_dropped_from_totals():
    attempts = list(paired())
    attempts[3] = replace(
        attempts[3],
        status=Outcome.INDETERMINATE,
        receipt_digest=None,
        money_microunits=700,
        duration_ms=None,
    )
    report = evaluate_benchmark(tuple(attempts), checker=FixtureChecker())
    portfolio = report["cohorts"]["portfolio"]
    assert portfolio["verified_success_rate"] is None
    assert portfolio["indeterminate_cases"] == 1
    assert portfolio["measured_money_microunits"] == 720
    assert portfolio["duration_unknown_attempts"] == 1
    assert portfolio["money_unknown_attempts"] == 0
    assert portfolio["usage_complete"] is False
    assert portfolio["usage_is_lower_bound"] is True


def test_unknown_costs_and_unchecked_receipts_still_count_as_usage():
    attempts = list(paired())
    attempts[3] = replace(
        attempts[3], status=Outcome.INDETERMINATE, receipt_digest=None, money_microunits=None
    )
    report = evaluate_benchmark(tuple(attempts))
    portfolio = report["cohorts"]["portfolio"]
    assert portfolio["verified_success_rate"] is None
    assert portfolio["measured_money_microunits"] == 20
    assert portfolio["money_unknown_attempts"] == 1
    assert portfolio["usage_complete"] is False
