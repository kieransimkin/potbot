"""Compare actual test receipts without turning synthetic fixtures into model scores.

This read-only evaluator does not execute code, call providers, settle budgets,
or verify receipts by itself. A trusted receipt checker must be explicitly
supplied to qualify externally recorded outcomes; otherwise results are labelled
unverified and success estimates are withheld.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.domain.serialization import content_hash

_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class Outcome(StrEnum):
    VERIFIED = "verified"
    FAILED = "failed"
    BLOCKED = "blocked"
    INDETERMINATE = "indeterminate"


class SampleKind(StrEnum):
    SYNTHETIC_FIXTURE = "synthetic_fixture"
    LIVE_DEPLOYMENT = "live_deployment"


@dataclass(frozen=True, slots=True)
class BenchmarkSample:
    """One bounded attempt as reported by a persisted execution ledger."""

    attempt_id: UUID
    case_id: str
    cohort: str
    ordinal: int
    model_id: str
    model_revision: str
    task_revision_digest: str
    repository_state_digest: str
    test_suite_digest: str
    receipt_digest: str | None
    status: Outcome
    kind: SampleKind
    duration_ms: int | None
    money_microunits: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, UUID):
            raise InvalidInputError("benchmark attempt requires UUID identity")
        for name in ("case_id", "cohort", "model_id", "model_revision"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 256:
                raise InvalidInputError(f"invalid benchmark {name}")
        if type(self.ordinal) is not int or not 1 <= self.ordinal <= 64:
            raise InvalidInputError("benchmark ordinal must be bounded")
        for name in ("task_revision_digest", "repository_state_digest", "test_suite_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
                raise InvalidInputError(f"invalid benchmark {name}")
        if self.receipt_digest is not None and (
            not isinstance(self.receipt_digest, str) or _DIGEST.fullmatch(self.receipt_digest) is None
        ):
            raise InvalidInputError("invalid benchmark verification receipt")
        if not isinstance(self.status, Outcome) or not isinstance(self.kind, SampleKind):
            raise InvalidInputError("invalid benchmark classification")
        if self.status in {Outcome.VERIFIED, Outcome.FAILED} and self.receipt_digest is None:
            raise InvalidInputError("reported verification requires an inspectable receipt")
        for name in ("duration_ms", "money_microunits"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 0 <= value <= 10**15):
                raise InvalidInputError(f"benchmark {name} is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "attempt_id": str(self.attempt_id), "case_id": self.case_id,
            "cohort": self.cohort, "ordinal": self.ordinal,
            "model_id": self.model_id, "model_revision": self.model_revision,
            "task_revision_digest": self.task_revision_digest,
            "repository_state_digest": self.repository_state_digest,
            "test_suite_digest": self.test_suite_digest,
            "receipt_digest": self.receipt_digest, "status": self.status.value,
            "kind": self.kind.value, "duration_ms": self.duration_ms,
            "money_microunits": self.money_microunits,
        }


    @classmethod
    def from_dict(cls, value: object) -> BenchmarkSample:
        required = {
            "attempt_id", "case_id", "cohort", "ordinal", "model_id",
            "model_revision", "task_revision_digest", "repository_state_digest",
            "test_suite_digest", "receipt_digest", "status", "kind",
            "duration_ms", "money_microunits",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise InvalidInputError("benchmark sample fields are invalid")
        try:
            identity = UUID(value["attempt_id"])
            if str(identity) != value["attempt_id"]:
                raise ValueError
            return cls(
                attempt_id=identity,
                case_id=value["case_id"], cohort=value["cohort"],
                ordinal=value["ordinal"], model_id=value["model_id"],
                model_revision=value["model_revision"],
                task_revision_digest=value["task_revision_digest"],
                repository_state_digest=value["repository_state_digest"],
                test_suite_digest=value["test_suite_digest"],
                receipt_digest=value["receipt_digest"],
                status=Outcome(value["status"]), kind=SampleKind(value["kind"]),
                duration_ms=value["duration_ms"],
                money_microunits=value["money_microunits"],
            )
        except (ValueError, TypeError, KeyError):
            raise InvalidInputError("invalid benchmark sample") from None


class TrustedReceiptChecker(Protocol):
    """Rechecks actual, scoped, exact-state checks in Potbot's evidence store."""

    def qualified(self, sample: BenchmarkSample) -> bool: ...


def _wilson(success: int, total: int) -> tuple[float, float] | None:
    if total == 0:
        return None
    z = 1.959963984540054
    p = success / total
    d = 1 + z * z / total
    center = (p + z * z / (2 * total)) / d
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / d
    return round(max(0, center - margin), 5), round(min(1, center + margin), 5)


def evaluate_benchmark(
    samples: tuple[BenchmarkSample, ...], *, checker: TrustedReceiptChecker | None = None,
    baseline_cohort: str | None = None, treatment_cohort: str | None = None
) -> dict[str, object]:
    """Score paired, case-identical cohorts; never assume absent usage is zero."""
    if not isinstance(samples, tuple) or not samples or len(samples) > 50_000:
        raise InvalidInputError("benchmark requires a finite set of bounded samples")
    seen: set[UUID] = set()
    grouped: dict[str, dict[str, list[BenchmarkSample]]] = defaultdict(lambda: defaultdict(list))
    specifications: dict[str, tuple[str, str, str]] = {}
    kinds: set[SampleKind] = set()
    for sample in samples:
        if not isinstance(sample, BenchmarkSample) or sample.attempt_id in seen:
            raise InvalidInputError("duplicate or malformed benchmark attempt")
        seen.add(sample.attempt_id)
        kinds.add(sample.kind)
        spec = (
            sample.task_revision_digest, sample.repository_state_digest, sample.test_suite_digest
        )
        if sample.case_id in specifications and specifications[sample.case_id] != spec:
            raise StateMismatchError("benchmark cohorts use different task or test revisions")
        specifications[sample.case_id] = spec
        grouped[sample.cohort][sample.case_id].append(sample)
    if len(kinds) != 1:
        raise StateMismatchError("synthetic fixtures and live model results cannot be combined")
    if len(grouped) > 16:
        raise InvalidInputError("too many benchmark cohorts")
    cases = set(specifications)
    arm_results: dict[str, dict[str, object]] = {}
    for cohort, rows in sorted(grouped.items()):
        if set(rows) != cases:
            raise StateMismatchError("cohorts must evaluate the identical case set")
        completed_cases = 0
        successes = 0
        reported_successes = 0
        pending = 0
        durations: list[int] = []
        costs: list[int] = []
        missing_duration = 0
        missing_cost = 0
        total_attempts = 0
        checked_case_results: dict[str, bool] = {}
        for case_id, attempts in sorted(rows.items()):
            attempts.sort(key=lambda x: x.ordinal)
            if [x.ordinal for x in attempts] != list(range(1, len(attempts) + 1)):
                raise StateMismatchError("attempt ordinal sequence is incomplete or duplicated")
            total_attempts += len(attempts)
            reported_successes += any(x.status == Outcome.VERIFIED for x in attempts)
            # Record *all* attempts before restricting success qualification.
            # A timed-out or indeterminate provider request may still be billed;
            # omitting its usage would systematically favor risky portfolios.
            for attempt in attempts:
                if attempt.duration_ms is None:
                    missing_duration += 1
                else:
                    durations.append(attempt.duration_ms)
                if attempt.money_microunits is None:
                    missing_cost += 1
                else:
                    costs.append(attempt.money_microunits)
            # An uncertain outcome must not be treated as failed, nor can a
            # purported verification be accepted without the actual receipt.
            if any(x.status in {Outcome.INDETERMINATE, Outcome.BLOCKED} for x in attempts):
                pending += 1
                continue
            if checker is None or any(
                x.receipt_digest is None or not checker.qualified(x)
                for x in attempts
            ):
                continue
            completed_cases += 1
            verified = any(x.status == Outcome.VERIFIED for x in attempts)
            successes += verified
            checked_case_results[case_id] = verified
        arm_results[cohort] = {
            "n_cases": len(cases), "n_attempts": total_attempts,
            "receipt_qualified_cases": completed_cases,
            "reported_successes_not_trusted": reported_successes,
            "indeterminate_cases": pending,
            "verified_successes": successes if checker is not None else None,
            "verified_success_rate": (
                round(successes / completed_cases, 6)
                if checker is not None and completed_cases == len(cases) else None
            ),
            "wilson_95": (
                _wilson(successes, completed_cases)
                if checker is not None and completed_cases == len(cases) else None
            ),
            "measured_duration_ms": sum(durations) if durations else None,
            "duration_unknown_attempts": missing_duration,
            "measured_money_microunits": sum(costs) if costs else None,
            "money_unknown_attempts": missing_cost,
            "usage_complete": missing_cost == 0 and missing_duration == 0,
            "usage_is_lower_bound": bool(missing_cost or missing_duration),
            "_checked_cases": checked_case_results,
        }
    paired: dict[str, object] = {}
    cohorts = set(arm_results)
    if baseline_cohort is None and treatment_cohort is None and cohorts == {"single", "portfolio"}:
        baseline_cohort, treatment_cohort = "single", "portfolio"
    if (baseline_cohort is None) != (treatment_cohort is None):
        raise InvalidInputError("paired comparisons require explicit baseline and treatment")
    if baseline_cohort is not None and (
        baseline_cohort == treatment_cohort or {baseline_cohort, treatment_cohort} != cohorts
    ):
        raise StateMismatchError("comparison cohorts do not match the recorded arms")
    if baseline_cohort is not None and treatment_cohort is not None and checker is not None:
        a, b = baseline_cohort, treatment_cohort
        a_cases = arm_results[a]["_checked_cases"]
        b_cases = arm_results[b]["_checked_cases"]
        if isinstance(a_cases, dict) and isinstance(b_cases, dict) and set(a_cases) == set(b_cases) == cases:
            better = sum(not a_cases[c] and b_cases[c] for c in cases)
            worse = sum(a_cases[c] and not b_cases[c] for c in cases)
            paired = {
                "cohorts": [a, b], "n_paired": len(cases),
                "improved": better, "regressed": worse,
                "success_difference": round((better - worse) / len(cases), 6),
            }
    for result in arm_results.values():
        result.pop("_checked_cases", None)
    return {
        "schema_version": 1,
        "classification": next(iter(kinds)).value,
        "qualification": "checked_external_receipts" if checker is not None else "unverified_claims",
        "dataset_identity": content_hash({k: v for k, v in sorted(specifications.items())}),
        "cohorts": arm_results,
        "paired_comparison": paired,
        "limitation": (
            "Receipt checks attest execution identity, not unbiased sampling or task difficulty."
            if checker is not None else
            "Reported outcomes are unverified; no model success rate is published."
        ),
    }
