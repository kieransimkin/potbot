"""Stable check identities and explicit, conservative verification decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from potbot.domain.errors import InvalidInputError

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,95}\Z")
STATUSES = frozenset(
    {"passed", "failed", "blocked", "not_run", "inconclusive", "infrastructure_error"}
)


@dataclass(frozen=True, slots=True)
class CheckDefinition:
    check_id: str
    behavior_id: str
    tool_name: str
    requirement_ids: tuple[str, ...] = ()
    definition_paths: tuple[str, ...] = ()
    mandatory: bool = True
    authored_by: str = "installed"
    infrastructure_exit_codes: tuple[int, ...] = (69, 78)

    def __post_init__(self) -> None:
        for value in (self.check_id, self.behavior_id, self.tool_name):
            if not isinstance(value, str) or not _NAME.fullmatch(value):
                raise InvalidInputError("Checks require bounded stable identifiers.")
        for values in (self.requirement_ids, self.definition_paths):
            if (
                not isinstance(values, tuple)
                or len(values) > 128
                or any(not isinstance(value, str) for value in values)
                or len(set(values)) != len(values)
            ):
                raise InvalidInputError("Check references must be bounded distinct tuples.")
        if any(
            not isinstance(value, str) or not _NAME.fullmatch(value)
            for value in self.requirement_ids
        ):
            raise InvalidInputError("Check requirement IDs are invalid.")
        for path in self.definition_paths:
            if (
                not isinstance(path, str)
                or not path
                or len(path) > 2048
                or any(char in path for char in ("\\", ":", "\x00"))
                or any(
                    part in {"", ".", ".."} or part.lower() == ".git" for part in path.split("/")
                )
            ):
                raise InvalidInputError(
                    "Check definitions require literal repository-relative paths."
                )
        if (
            type(self.mandatory) is not bool
            or not isinstance(self.authored_by, str)
            or self.authored_by not in {"installed", "user", "agent"}
        ):
            raise InvalidInputError("Unknown check authority or requirement policy.")
        if (
            not isinstance(self.infrastructure_exit_codes, tuple)
            or len(self.infrastructure_exit_codes) > 16
            or any(
                type(code) is not int or not 1 <= code <= 255
                for code in self.infrastructure_exit_codes
            )
            or len(set(self.infrastructure_exit_codes)) != len(self.infrastructure_exit_codes)
        ):
            raise InvalidInputError("Infrastructure exit codes must be distinct nonzero codes.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "behavior_id": self.behavior_id,
            "tool_name": self.tool_name,
            "requirement_ids": list(self.requirement_ids),
            "definition_paths": list(self.definition_paths),
            "mandatory": self.mandatory,
            "authored_by": self.authored_by,
            "infrastructure_exit_codes": list(self.infrastructure_exit_codes),
        }


@dataclass(frozen=True, slots=True)
class VerificationProfile:
    name: str
    version: int
    checks: tuple[CheckDefinition, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not _NAME.fullmatch(self.name)
            or type(self.version) is not int
            or self.version < 1
        ):
            raise InvalidInputError("Verification profiles require a name and positive version.")
        if (
            not isinstance(self.checks, tuple)
            or not 1 <= len(self.checks) <= 128
            or not all(isinstance(check, CheckDefinition) for check in self.checks)
            or len({check.check_id for check in self.checks}) != len(self.checks)
        ):
            raise InvalidInputError(
                "Verification profiles require distinct bounded check definitions."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "name": self.name,
            "version": self.version,
            "checks": [check.to_dict() for check in self.checks],
        }


def comparison(baseline: str, candidate: str, *, comparable: bool = True) -> str:
    """A pre-existing failure stays failed; it is never relabelled as a pass."""
    if (
        not isinstance(baseline, str)
        or not isinstance(candidate, str)
        or baseline not in STATUSES
        or candidate not in STATUSES
    ):
        raise InvalidInputError("Unknown verification status.")
    if type(comparable) is not bool:
        raise InvalidInputError("Check comparability must be explicit.")
    if not comparable:
        return "not_comparable"
    if baseline == "failed" and candidate == "passed":
        return "resolved"
    if baseline == "passed" and candidate == "failed":
        return "regression"
    if baseline == candidate == "failed":
        return "pre_existing_failure"
    if baseline == candidate == "passed":
        return "still_passed"
    return "incomplete"


def aggregate_status(statuses: list[str]) -> str:
    if not isinstance(statuses, list) or any(not isinstance(status, str) for status in statuses):
        raise InvalidInputError("Verification statuses must be an explicit list of status names.")
    if not statuses:
        return "not_run"
    if any(status not in STATUSES for status in statuses):
        raise InvalidInputError("Unknown verification status.")
    for status in ("failed", "infrastructure_error", "blocked", "inconclusive", "not_run"):
        if status in statuses:
            return status
    return "passed"
