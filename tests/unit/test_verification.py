from __future__ import annotations

from dataclasses import replace
from itertools import permutations
from typing import Any

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import content_hash
from potbot.domain.verification import (
    CheckDefinition,
    VerificationProfile,
    aggregate_status,
    comparison,
)


@pytest.mark.parametrize(
    "baseline,candidate,expected",
    [
        ("passed", "passed", "still_passed"),
        ("passed", "failed", "regression"),
        ("failed", "passed", "resolved"),
        ("failed", "failed", "pre_existing_failure"),
        ("not_run", "passed", "incomplete"),
        ("blocked", "passed", "incomplete"),
        ("infrastructure_error", "passed", "incomplete"),
        ("inconclusive", "passed", "incomplete"),
        ("passed", "not_run", "incomplete"),
        ("failed", "blocked", "incomplete"),
        ("infrastructure_error", "infrastructure_error", "incomplete"),
    ],
)
def test_baseline_comparison_never_converts_missing_or_failed_evidence_into_a_pass(
    baseline: str,
    candidate: str,
    expected: str,
) -> None:
    assert comparison(baseline, candidate) == expected


@pytest.mark.parametrize(
    "baseline,candidate",
    [("passed", "passed"), ("failed", "passed"), ("passed", "failed"), ("not_run", "passed")],
)
def test_changed_definitions_or_environment_make_results_non_comparable(
    baseline: str,
    candidate: str,
) -> None:
    assert comparison(baseline, candidate, comparable=False) == "not_comparable"


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([], "not_run"),
        (["passed", "passed"], "passed"),
        (["passed", "failed"], "failed"),
        (["failed", "infrastructure_error", "blocked"], "failed"),
        (["passed", "infrastructure_error", "blocked"], "infrastructure_error"),
        (["passed", "blocked", "not_run"], "blocked"),
        (["passed", "inconclusive", "not_run"], "inconclusive"),
        (["passed", "not_run"], "not_run"),
    ],
)
def test_status_aggregation_preserves_failures_and_incomplete_evidence_in_any_order(
    statuses: list[str],
    expected: str,
) -> None:
    for ordered in set(permutations(statuses)):
        assert aggregate_status(list(ordered)) == expected


@pytest.mark.parametrize("invalid", ["skipped", "timeout", None, {}, [], 1, True])
def test_invalid_observation_statuses_raise_domain_errors(invalid: Any) -> None:
    with pytest.raises(InvalidInputError):
        comparison(invalid, "passed")
    with pytest.raises(InvalidInputError):
        comparison("passed", invalid)
    with pytest.raises(InvalidInputError):
        aggregate_status(["passed", invalid])


@pytest.mark.parametrize("invalid", [None, (), "", "passed", {}])
def test_aggregation_requires_an_explicit_status_list(invalid: Any) -> None:
    with pytest.raises(InvalidInputError):
        aggregate_status(invalid)


@pytest.mark.parametrize("invalid", [0, 1, "false", None, []])
def test_comparability_cannot_be_inferred_from_truthy_values(invalid: Any) -> None:
    with pytest.raises(InvalidInputError):
        comparison("passed", "passed", comparable=invalid)


@pytest.fixture
def check() -> CheckDefinition:
    return CheckDefinition(
        "unit.tests",
        "invoice.calculation",
        "run-tests",
        requirement_ids=("correct-total",),
        definition_paths=("tests/test_totals.py",),
    )


@pytest.mark.parametrize(
    "change",
    [
        {"check_id": ""},
        {"behavior_id": "invalid behavior"},
        {"tool_name": "x" * 97},
        {"tool_name": []},
        {"requirement_ids": ["criterion"]},
        {"requirement_ids": ("criterion", "criterion")},
        {"requirement_ids": ([],)},
        {"requirement_ids": ("bad id",)},
        {"definition_paths": ["tests/test.py"]},
        {"definition_paths": ({},)},
        {"definition_paths": ("tests/test.py", "tests/test.py")},
        {"definition_paths": ("/absolute.py",)},
        {"definition_paths": ("tests/../outside.py",)},
        {"definition_paths": ("tests/./test.py",)},
        {"definition_paths": (".GiT/hooks/pre-commit",)},
        {"definition_paths": ("tests//test.py",)},
        {"definition_paths": ("C:\\tests\\test.py",)},
        {"definition_paths": ("tests\x00/test.py",)},
        {"mandatory": 1},
        {"authored_by": []},
        {"authored_by": "model"},
        {"infrastructure_exit_codes": [69]},
        {"infrastructure_exit_codes": (0,)},
        {"infrastructure_exit_codes": (256,)},
        {"infrastructure_exit_codes": (True,)},
        {"infrastructure_exit_codes": (69, 69)},
        {"infrastructure_exit_codes": ([],)},
        {"infrastructure_exit_codes": tuple(range(1, 18))},
    ],
)
def test_invalid_check_definitions_are_rejected_before_persistence(
    check: CheckDefinition,
    change: dict[str, Any],
) -> None:
    with pytest.raises(InvalidInputError):
        replace(check, **change)


@pytest.mark.parametrize(
    "change",
    [
        {"name": ""},
        {"name": "profile with spaces"},
        {"version": 0},
        {"version": True},
        {"checks": ()},
        {"checks": [CheckDefinition("a", "behavior", "tool")]},
        {"checks": ({"check_id": "a"},)},
        {
            "checks": (
                CheckDefinition("a", "behavior", "tool"),
                CheckDefinition("a", "other", "tool"),
            )
        },
        {
            "checks": tuple(
                CheckDefinition(f"check{index}", "behavior", "tool") for index in range(129)
            )
        },
    ],
)
def test_invalid_profiles_cannot_supply_verification_evidence(
    check: CheckDefinition,
    change: dict[str, Any],
) -> None:
    profile = VerificationProfile("default", 1, (check,))
    with pytest.raises(InvalidInputError):
        replace(profile, **change)


def test_profile_identity_captures_authority_requirement_and_definition_changes(
    check: CheckDefinition,
) -> None:
    profile = VerificationProfile("default", 1, (check,))
    original = content_hash(profile.to_dict())
    for changed in (
        replace(check, authored_by="agent"),
        replace(check, mandatory=False),
        replace(check, requirement_ids=("other-criterion",)),
        replace(check, definition_paths=("tests/test_other.py",)),
        replace(check, infrastructure_exit_codes=(78,)),
    ):
        assert content_hash(replace(profile, checks=(changed,)).to_dict()) != original
    assert profile.to_dict()["checks"][0]["requirement_ids"] == ["correct-total"]
    assert profile.to_dict()["checks"][0]["authored_by"] == "installed"
