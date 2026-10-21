"""Task contract validation and lifecycle invariants without infrastructure."""

from dataclasses import FrozenInstanceError, replace

import pytest

from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.domain.tasks import (
    ContractSpec,
    DeliverableType,
    Requirement,
    TaskStatus,
    check_transition,
)


def contract() -> ContractSpec:
    return ContractSpec(
        objective="Repair cancellation cleanup",
        baseline_ref="git:" + "a" * 40,
        deliverable_type=DeliverableType.PATCH,
        requirements=(Requirement("R1", "Release the connection", "Cancellation closes once"),),
        change_scope=("src/client.py",),
        capabilities=("compile", "test"),
        verification_policy="python-unit-v1",
        budget=(("tokens", 1000), ("calls", 10), ("tool_ms", 5000), ("concurrency", 2)),
        compatibility_constraints=("Preserve the public API",),
        nonfunctional_requirements=("No additional network round trips",),
    )


def test_contract_round_trip_retains_criteria_constraints_and_is_deeply_immutable() -> None:
    value = contract()
    payload = value.to_dict()
    assert ContractSpec.from_dict(payload) == value
    payload["budget"]["tokens"] = 0
    payload["requirements"][0]["description"] = "changed"
    assert dict(value.budget)["tokens"] == 1000
    assert value.requirements[0].description == "Release the connection"
    with pytest.raises(FrozenInstanceError):
        value.objective = "different"  # type: ignore[misc]


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"deliverable_type": "pull_request"},
        {"budget": {"tokens": 10}},
        {"budget": {"tokens": True, "calls": 1, "tool_ms": 1, "concurrency": 1}},
        {"budget": {"tokens": -1, "calls": 1, "tool_ms": 1, "concurrency": 1}},
        {"budget": {"tokens": 10, "calls": 1, "tool_ms": 1, "concurrency": 0}},
        {"change_scope": []},
        {"requirements": []},
        {"objective": ""},
    ],
)
def test_invalid_contract_never_reaches_initialization(change: dict) -> None:
    payload = contract().to_dict()
    payload.update(change)
    with pytest.raises(InvalidInputError):
        ContractSpec.from_dict(payload)


def test_missing_budget_unknown_fields_and_duplicate_criteria_are_rejected() -> None:
    payload = contract().to_dict()
    del payload["budget"]
    with pytest.raises(InvalidInputError):
        ContractSpec.from_dict(payload)
    payload = contract().to_dict() | {"workspace_id": "untrusted"}
    with pytest.raises(InvalidInputError):
        ContractSpec.from_dict(payload)
    with pytest.raises(InvalidInputError):
        replace(contract(), requirements=(contract().requirements[0],) * 2)


def test_investigation_can_declare_empty_change_scope() -> None:
    value = replace(contract(), deliverable_type=DeliverableType.INVESTIGATION, change_scope=())
    assert ContractSpec.from_dict(value.to_dict()) == value


@pytest.mark.parametrize(
    "current,target",
    [
        (TaskStatus.SUBMITTED, TaskStatus.RUNNING),
        (TaskStatus.PAUSED, TaskStatus.RUNNING),
        (TaskStatus.BLOCKED, TaskStatus.RUNNING),
        (TaskStatus.VERIFIED, TaskStatus.RUNNING),
        (TaskStatus.BUDGET_EXHAUSTED, TaskStatus.RUNNING),
        (TaskStatus.CANCELLED, TaskStatus.INITIALIZING),
    ],
)
def test_invalid_lifecycle_shortcuts_are_rejected(current: TaskStatus, target: TaskStatus) -> None:
    with pytest.raises(StateMismatchError):
        check_transition(current, target)
