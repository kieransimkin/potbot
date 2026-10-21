"""Immutable task requests and the finite task lifecycle; no adapter imports."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from potbot.domain.errors import InvalidInputError, StateMismatchError

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,95}\Z")
MANDATORY_BUDGET_DIMENSIONS = frozenset({"tokens", "calls", "tool_ms", "concurrency"})


class DeliverableType(StrEnum):
    PATCH = "patch"
    INVESTIGATION = "investigation"


class TaskStatus(StrEnum):
    SUBMITTED = "submitted"
    INITIALIZING = "initializing"
    RUNNING = "running"
    PAUSED = "paused"
    BLOCKED = "blocked"
    VERIFIED = "verified"
    PARTIAL = "partial"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"
    FAILED = "failed"


TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.VERIFIED,
        TaskStatus.PARTIAL,
        TaskStatus.BUDGET_EXHAUSTED,
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
    }
)


def nonempty(value: object, name: str, *, limit: int = 16000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise InvalidInputError(f"{name} must be a nonempty string of at most {limit} characters")
    return value


def string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise InvalidInputError(f"{name} must be an immutable tuple")
    return tuple(nonempty(item, name) for item in value)


def validate_amounts(value: Mapping[str, int], *, mandatory: bool = False) -> dict[str, int]:
    if not isinstance(value, Mapping) or not value:
        raise InvalidInputError("resource amounts must be a nonempty mapping")
    result: dict[str, int] = {}
    for dimension, amount in value.items():
        if (
            not isinstance(dimension, str)
            or not _NAME.fullmatch(dimension)
            or type(amount) is not int
            or not 0 <= amount <= 2**53 - 1
        ):
            raise InvalidInputError(
                "resource dimensions require names and nonnegative safe integers"
            )
        result[dimension] = amount
    if mandatory and not MANDATORY_BUDGET_DIMENSIONS.issubset(result):
        raise InvalidInputError("budget requires tokens, calls, tool_ms, and concurrency limits")
    if "concurrency" in result and result["concurrency"] < 1:
        raise InvalidInputError("concurrency must be positive")
    return result


@dataclass(frozen=True, slots=True)
class Requirement:
    id: str
    description: str
    acceptance_criterion: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _NAME.fullmatch(self.id):
            raise InvalidInputError("requirement id must be a stable identifier")
        nonempty(self.description, "requirement description")
        nonempty(self.acceptance_criterion, "acceptance criterion")

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "description": self.description,
            "acceptance_criterion": self.acceptance_criterion,
        }

    @classmethod
    def from_dict(cls, value: object) -> Requirement:
        if not isinstance(value, dict) or set(value) != {
            "id",
            "description",
            "acceptance_criterion",
        }:
            raise InvalidInputError("requirement fields do not match schema")
        return cls(value["id"], value["description"], value["acceptance_criterion"])


@dataclass(frozen=True, slots=True)
class ContractSpec:
    objective: str
    baseline_ref: str
    deliverable_type: DeliverableType
    requirements: tuple[Requirement, ...]
    change_scope: tuple[str, ...]
    capabilities: tuple[str, ...]
    verification_policy: str
    budget: tuple[tuple[str, int], ...]
    compatibility_constraints: tuple[str, ...] = ()
    nonfunctional_requirements: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise InvalidInputError("unsupported contract schema_version")
        nonempty(self.objective, "objective")
        nonempty(self.baseline_ref, "baseline_ref")
        if not isinstance(self.deliverable_type, DeliverableType):
            raise InvalidInputError("deliverable_type must be patch or investigation")
        if (
            not isinstance(self.requirements, tuple)
            or not self.requirements
            or not all(isinstance(item, Requirement) for item in self.requirements)
            or len({item.id for item in self.requirements}) != len(self.requirements)
        ):
            raise InvalidInputError("requirements need unique stable IDs and acceptance criteria")
        for name in (
            "change_scope",
            "capabilities",
            "compatibility_constraints",
            "nonfunctional_requirements",
        ):
            string_tuple(getattr(self, name), name)
        if self.deliverable_type is DeliverableType.PATCH and not self.change_scope:
            raise InvalidInputError("patch contracts require an explicit change_scope")
        nonempty(self.verification_policy, "verification_policy")
        if not isinstance(self.budget, tuple) or not all(
            isinstance(item, tuple) and len(item) == 2 for item in self.budget
        ):
            raise InvalidInputError("budget must be an immutable tuple of dimension/limit pairs")
        try:
            dimensions = dict(self.budget)
        except (TypeError, ValueError):
            raise InvalidInputError("invalid budget dimensions") from None
        if len(dimensions) != len(self.budget):
            raise InvalidInputError("budget dimensions must be unique")
        validate_amounts(dimensions, mandatory=True)
        object.__setattr__(self, "budget", tuple(sorted(dimensions.items())))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "objective": self.objective,
            "baseline_ref": self.baseline_ref,
            "deliverable_type": self.deliverable_type.value,
            "requirements": [item.to_dict() for item in self.requirements],
            "change_scope": list(self.change_scope),
            "capabilities": list(self.capabilities),
            "verification_policy": self.verification_policy,
            "budget": dict(self.budget),
            "compatibility_constraints": list(self.compatibility_constraints),
            "nonfunctional_requirements": list(self.nonfunctional_requirements),
        }

    @classmethod
    def from_dict(cls, value: object) -> ContractSpec:
        fields = {
            "schema_version",
            "objective",
            "baseline_ref",
            "deliverable_type",
            "requirements",
            "change_scope",
            "capabilities",
            "verification_policy",
            "budget",
            "compatibility_constraints",
            "nonfunctional_requirements",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise InvalidInputError("contract fields do not match schema; budget is mandatory")
        for name in (
            "requirements",
            "change_scope",
            "capabilities",
            "compatibility_constraints",
            "nonfunctional_requirements",
        ):
            if not isinstance(value[name], list):
                raise InvalidInputError(f"{name} must be an array")
        if not isinstance(value["budget"], dict):
            raise InvalidInputError("budget must be an object")
        try:
            deliverable = DeliverableType(value["deliverable_type"])
        except (ValueError, TypeError):
            raise InvalidInputError("deliverable_type must be patch or investigation") from None
        return cls(
            objective=value["objective"],
            baseline_ref=value["baseline_ref"],
            deliverable_type=deliverable,
            requirements=tuple(Requirement.from_dict(item) for item in value["requirements"]),
            change_scope=tuple(value["change_scope"]),
            capabilities=tuple(value["capabilities"]),
            verification_policy=value["verification_policy"],
            budget=tuple(value["budget"].items()),
            compatibility_constraints=tuple(value["compatibility_constraints"]),
            nonfunctional_requirements=tuple(value["nonfunctional_requirements"]),
            schema_version=value["schema_version"],
        )


def check_transition(current: TaskStatus, target: TaskStatus) -> None:
    """Terminal records are final; suspended records require explicit resume."""
    if current in TERMINAL_STATUSES or current == target:
        raise StateMismatchError("task lifecycle transition is not allowed")
    allowed = {
        TaskStatus.SUBMITTED: {
            TaskStatus.INITIALIZING,
            TaskStatus.PAUSED,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.BUDGET_EXHAUSTED,
        },
        TaskStatus.INITIALIZING: {
            TaskStatus.RUNNING,
            TaskStatus.PAUSED,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.BUDGET_EXHAUSTED,
        },
        TaskStatus.RUNNING: {
            TaskStatus.PAUSED,
            TaskStatus.BLOCKED,
            TaskStatus.VERIFIED,
            TaskStatus.PARTIAL,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.BUDGET_EXHAUSTED,
        },
        TaskStatus.PAUSED: {TaskStatus.CANCELLED, TaskStatus.FAILED},
        TaskStatus.BLOCKED: {TaskStatus.CANCELLED, TaskStatus.FAILED},
    }
    if target not in allowed.get(current, set()):
        raise StateMismatchError("task lifecycle transition is not allowed")
