"""Deterministic authored-fixture benchmark acceptance, not live-model qualification."""

from potbot.evaluation.phase6_bench import CASES, benchmark


def test_first_candidate_baseline_and_two_candidate_search():
    baseline = benchmark(max_candidates=1)
    search = benchmark(max_candidates=2)
    assert baseline["classification"] == "synthetic-fixture; not a live model evaluation"
    assert search["model_providers_tested"] == []
    assert baseline["n_cases"] == search["n_cases"] == len(CASES) == 6
    assert baseline["baseline_failures"] == search["baseline_failures"] == 6
    assert baseline["n_attempts"] == 6
    assert search["n_attempts"] == 12
    assert baseline["observed_success_by_1"] == search["observed_success_by_1"] == 0.0
    assert baseline["verified_successes"] == 0
    assert search["verified_successes"] == 5
    assert search["correct_stops"] == 6
    assert search["total_backtracks"] == 6
    assert search["search_policy"] == "deterministic-lifo-depth-first-v1"
    assert all(c["search"]["claim_count"] == 2 for c in search["case_results"])
    assert all(c["scheduler_integrity"] for c in search["case_results"])
    assert all(
        len(a["cog_receipt_chain"]) == 3 for c in search["case_results"] for a in c["attempts"]
    )
    assert (
        next(c for c in search["case_results"] if c["case_id"] == "unresolved")["verified"] is False
    )
    assert (
        next(c for c in search["case_results"] if c["case_id"] == "reuse")["attempts"][1]["reuse"]
        is True
    )
