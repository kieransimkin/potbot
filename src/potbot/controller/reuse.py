"""Pure planning and structural review for the reuse-before-coding policy.

Callers inspect repository dependencies/configuration, execute the intents, and
record their results. This Phase 1 module performs no search, I/O, inference, or
source verification; it cannot certify that supplied attempt records are genuine.
"""

from __future__ import annotations

from collections.abc import Sequence

from potbot.domain.errors import InvalidInputError
from potbot.domain.reuse import (
    ReuseAssessment,
    ReuseDecision,
    ReusePlan,
    ReuseScope,
    SearchChannel,
    SearchEvidence,
    SearchIntent,
)


def plan_reuse_search(
    scope: ReuseScope,
    task_summary: str,
    context_terms: tuple[str, ...] = (),
    *,
    max_results_per_channel: int = 5,
) -> ReusePlan:
    """Plan one bounded, task-specific query in each required discovery channel.

    Context terms should describe the repository's language, dependencies, and
    constraints. Scope travels as explicit metadata, never as external query text.
    Scope identifies the requesting context. Internal adapters must enforce
    workspace permissions and project/shared-workflow visibility rules.
    """
    if not isinstance(task_summary, str) or not task_summary.strip():
        raise InvalidInputError("task_summary must be a nonempty string")
    if not isinstance(context_terms, tuple) or not all(
        isinstance(term, str) and term.strip() for term in context_terms
    ):
        raise InvalidInputError("context_terms must be an immutable tuple of nonempty strings")
    subject = " ".join(task_summary.split())
    if context_terms:
        subject += "; context: " + ", ".join(" ".join(term.split()) for term in context_terms)
    queries = {
        SearchChannel.EXTERNAL_LIBRARIES: (
            f"{subject}; existing libraries or packages; official documentation and example code"
        ),
        SearchChannel.EXTERNAL_TOOLS: (
            f"{subject}; existing developer tools or command line utilities; usage examples"
        ),
        SearchChannel.INTERNAL_WORKFLOWS: (
            f"{subject}; reusable internal workflows, procedures, examples, and successful prior tasks"
        ),
    }
    return ReusePlan(
        scope=scope,
        task_summary=task_summary,
        intents=tuple(
            SearchIntent(scope, channel, query, max_results_per_channel)
            for channel, query in queries.items()
        ),
    )


def review_reuse_plan(
    plan: ReusePlan,
    searches: Sequence[SearchEvidence],
    decision: ReuseDecision,
) -> ReuseAssessment:
    """Reject incomplete/inconsistent records before a caller proceeds to coding.

    Completed searches may find nothing. Unavailable or budget-limited searches
    need an explicit reason and attempt reference, and remain visibly degraded.
    An assessment establishes structural readiness only, not source trust or
    authorization to execute code. Future orchestration must call this gate.
    """
    if not isinstance(searches, Sequence) or isinstance(searches, (str, bytes)):
        raise InvalidInputError("searches must be a sequence of SearchEvidence records")
    return ReuseAssessment(plan, tuple(searches), decision)
