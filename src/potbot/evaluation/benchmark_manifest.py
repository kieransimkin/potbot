"""Immutable held-out benchmark suite identity and paired run admissibility.

Manifests are an *input* to evaluation, not proof that hidden tests stayed hidden
or that a model was executed. Actual benchmark scores additionally require a
trusted verification-receipt checker and task execution evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.domain.serialization import content_hash
from potbot.evaluation.benchmark_ledger import BenchmarkSample, SampleKind

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")


def _digest(value: object, name: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise InvalidInputError(f"{name} requires a SHA-256 identity")


def _identifier(value: object, name: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise InvalidInputError(f"{name} must be a bounded identifier")


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    """The held-out evaluator is identified, never serialized as model input."""

    case_id: str
    task_revision_digest: str
    repository_state_digest: str
    test_suite_digest: str
    model_input_digest: str
    hidden_evaluator_digest: str

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        for name in (
            "task_revision_digest", "repository_state_digest", "test_suite_digest",
            "model_input_digest", "hidden_evaluator_digest",
        ):
            _digest(getattr(self, name), name)

    def to_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: object) -> BenchmarkCase:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise InvalidInputError("benchmark case fields are invalid")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class BenchmarkSuite:
    suite_id: str
    kind: SampleKind
    cases: tuple[BenchmarkCase, ...]
    cohort_limits: tuple[tuple[str, int], ...]
    model_ids: tuple[str, ...]
    randomization_digest: str
    max_money_microunits: int
    schema_version: int = 1

    def __post_init__(self) -> None:
        _identifier(self.suite_id, "suite_id")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise InvalidInputError("unsupported benchmark suite schema")
        if not isinstance(self.kind, SampleKind):
            raise InvalidInputError("benchmark suite kind must be explicit")
        if (
            not isinstance(self.cases, tuple) or not 1 <= len(self.cases) <= 10000
            or any(not isinstance(case, BenchmarkCase) for case in self.cases)
            or len({case.case_id for case in self.cases}) != len(self.cases)
        ):
            raise InvalidInputError("benchmark suite cases must be distinct and bounded")
        if (
            not isinstance(self.cohort_limits, tuple)
            or not 1 <= len(self.cohort_limits) <= 16
            or any(
                not isinstance(row, tuple) or len(row) != 2
                or type(row[1]) is not int or not 1 <= row[1] <= 64
                for row in self.cohort_limits
            )
        ):
            raise InvalidInputError("benchmark cohort limits are invalid")
        for name, _ in self.cohort_limits:
            _identifier(name, "cohort")
        if len({name for name, _ in self.cohort_limits}) != len(self.cohort_limits):
            raise InvalidInputError("benchmark cohort limits must be distinct")
        if (
            not isinstance(self.model_ids, tuple)
            or not 1 <= len(self.model_ids) <= 32
            or any(not isinstance(m, str) or not m or len(m) > 256 for m in self.model_ids)
            or len(set(self.model_ids)) != len(self.model_ids)
        ):
            raise InvalidInputError("benchmark models must be selected explicitly")
        _digest(self.randomization_digest, "randomization_digest")
        if (type(self.max_money_microunits) is not int
                or not 0 <= self.max_money_microunits <= 10**15):
            raise InvalidInputError("benchmark cost ceiling is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "suite_id": self.suite_id,
            "kind": self.kind.value,
            "cases": [case.to_dict() for case in self.cases],
            "cohort_limits": [list(row) for row in self.cohort_limits],
            "model_ids": list(self.model_ids),
            "randomization_digest": self.randomization_digest,
            "max_money_microunits": self.max_money_microunits,
        }

    @classmethod
    def from_dict(cls, value: object) -> BenchmarkSuite:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise InvalidInputError("benchmark suite fields are invalid")
        for name in ("cases", "cohort_limits", "model_ids"):
            if not isinstance(value[name], list):
                raise InvalidInputError("benchmark suite arrays must be JSON lists")
        try:
            return cls(
                schema_version=value["schema_version"],
                suite_id=value["suite_id"],
                kind=SampleKind(value["kind"]),
                cases=tuple(BenchmarkCase.from_dict(item) for item in value["cases"]),
                cohort_limits=tuple(
                    tuple(row) if isinstance(row, list) else row
                    for row in value["cohort_limits"]
                ),
                model_ids=tuple(value["model_ids"]),
                randomization_digest=value["randomization_digest"],
                max_money_microunits=value["max_money_microunits"],
            )
        except (TypeError, ValueError, KeyError):
            raise InvalidInputError("benchmark suite contains invalid values") from None

    @property
    def digest(self) -> str:
        return content_hash(self.to_dict())


def check_suite_samples(suite: BenchmarkSuite, samples: tuple[BenchmarkSample, ...]) -> str:
    """Reject cohort leakage, mismatched task versions and over-budget runs.

    Returns the pinned manifest hash when every case/cohort combination is
    represented. Missing receipt/usage values remain reportable but unqualified.
    """
    if not isinstance(suite, BenchmarkSuite) or not isinstance(samples, tuple):
        raise InvalidInputError("benchmark requires a typed suite and attempt collection")
    cases = {case.case_id: case for case in suite.cases}
    cohorts = dict(suite.cohort_limits)
    counts: dict[tuple[str, str], set[int]] = {}
    total_known_cost = 0
    for item in samples:
        if not isinstance(item, BenchmarkSample):
            raise InvalidInputError("malformed benchmark attempt")
        case = cases.get(item.case_id)
        if case is None or item.cohort not in cohorts or item.model_id not in suite.model_ids:
            raise StateMismatchError("benchmark attempt is outside its registered suite")
        if item.kind != suite.kind or (
            item.task_revision_digest, item.repository_state_digest, item.test_suite_digest
        ) != (
            case.task_revision_digest, case.repository_state_digest, case.test_suite_digest
        ):
            raise StateMismatchError("benchmark attempt used another case or classification")
        if item.ordinal > cohorts[item.cohort]:
            raise StateMismatchError("benchmark attempt exceeded its cohort allowance")
        indices = counts.setdefault((item.cohort, item.case_id), set())
        if item.ordinal in indices:
            raise StateMismatchError("benchmark repeated a cohort/case ordinal")
        indices.add(item.ordinal)
        if item.money_microunits is not None:
            total_known_cost += item.money_microunits
            if total_known_cost > suite.max_money_microunits:
                raise StateMismatchError("benchmark exceeded its declared cost ceiling")
    if set(counts) != {(cohort, case) for cohort in cohorts for case in cases}:
        raise StateMismatchError("benchmark suite lacks one or more paired cases")
    if any(indices != set(range(1, len(indices) + 1)) for indices in counts.values()):
        raise StateMismatchError("benchmark attempted a noncontiguous ordinal")
    return suite.digest
