"""Offline A/B/C prompt-profile evaluation with independent, held-out scoring.

Candidate prompts are never auto-promoted. The evaluator callback is supplied
by the trusted qualification harness, not by the model being evaluated.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import content_hash
from potbot.models.prompt_compiler import (
    CompiledPrompt,
    PromptVariant,
    ReasoningRole,
    compile_reasoning_prompt,
)


@dataclass(frozen=True, slots=True)
class PromptTrial:
    case_id: str
    task: str
    role: ReasoningRole
    expected: str

    def __post_init__(self) -> None:
        if (not isinstance(self.case_id, str) or not 1 <= len(self.case_id) <= 128
                or not isinstance(self.task, str) or not self.task or len(self.task) > 65536
                or not isinstance(self.expected, str) or not 1 <= len(self.expected) <= 4096):
            raise InvalidInputError("prompt trial requires bounded held-out evidence")


@dataclass(frozen=True, slots=True)
class PromptTrialResult:
    model: str
    case_id: str
    role: str
    variant: str
    prompt_hash: str
    passed: bool
    output_tokens: int
    output_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model, "case_id": self.case_id, "role": self.role,
            "variant": self.variant, "prompt_hash": self.prompt_hash,
            "passed": self.passed, "output_tokens": self.output_tokens,
            "output_hash": self.output_hash,
        }


@dataclass(frozen=True, slots=True)
class PromptQualification:
    results: tuple[PromptTrialResult, ...]
    evaluator_revision: str
    qualified: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = {"schema_version": 1, "evaluator_revision": self.evaluator_revision,
                   "qualified": False, "results": [r.to_dict() for r in self.results]}
        return {**payload, "sha256": content_hash(payload)}


def compare_prompt_variants(
    *, model: str, cases: Sequence[PromptTrial],
    generate: Callable[[CompiledPrompt], tuple[str, int]],
    evaluate: Callable[[PromptTrial, str], bool],
    evaluator_revision: str,
) -> PromptQualification:
    """Run the same cases against A generic, B family and C model-role prompts.

    This code is intentionally not hooked to the live inference endpoint: a
    qualified evaluation environment must supply test cases, model revisions,
    independent scoring and a separately authorized promotion step.
    """
    if not callable(generate) or not callable(evaluate) or not evaluator_revision:
        raise InvalidInputError("qualification requires an independent scoring harness")
    if not isinstance(cases, Sequence) or not 1 <= len(cases) <= 200 or any(
        not isinstance(case, PromptTrial) for case in cases
    ):
        raise InvalidInputError("qualification cases are missing or unbounded")
    if len({case.case_id for case in cases}) != len(cases):
        raise InvalidInputError("qualification case identities must be unique")
    findings: list[PromptTrialResult] = []
    variants: tuple[PromptVariant, ...] = ("generic", "family", "model_role")
    for case in cases:
        for variant in variants:
            packet = compile_reasoning_prompt(
                model=model, role=case.role, task=case.task,
                variant=variant,
            )
            output, tokens = generate(packet)
            if not isinstance(output, str) or type(tokens) is not int or tokens < 0:
                raise InvalidInputError("qualification requires measured model results")
            passed = evaluate(case, output)
            if type(passed) is not bool:
                raise InvalidInputError("qualification evaluator must return a boolean")
            findings.append(PromptTrialResult(
                model, case.case_id, case.role, variant, packet.identity,
                passed, tokens, content_hash(output),
            ))
    return PromptQualification(tuple(findings), evaluator_revision)
