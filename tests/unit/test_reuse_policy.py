"""Policy boundary tests: coverage, scope, provenance references, and degradation."""

import json
from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest

from potbot.controller.reuse import plan_reuse_search, review_reuse_plan
from potbot.domain import (
    InvalidInputError,
    ReuseAction,
    ReuseAssessment,
    ReuseCandidate,
    ReuseDecision,
    ReusePlan,
    ReuseScope,
    ReviewStatus,
    SearchChannel,
    SearchEvidence,
    SearchStatus,
)


@pytest.fixture
def plan() -> ReusePlan:
    return plan_reuse_search(
        ReuseScope(uuid4(), uuid4(), uuid4()),
        "Add deterministic JSON schema validation",
        ("Python 3.12", "existing stdlib dataclasses", "offline execution"),
    )


@pytest.fixture
def candidate() -> ReuseCandidate:
    return ReuseCandidate(
        uuid4(),
        "Existing schema validation workflow",
        "workflow://current-workspace/current-project/schema-validation/revision-2",
        "Preserves existing error codes and requires no runtime dependency; adapt field rules.",
    )


def completed_searches(plan: ReusePlan) -> tuple[SearchEvidence, ...]:
    return tuple(
        SearchEvidence(intent, SearchStatus.COMPLETED, f"attempt://{intent.channel.value}/1")
        for intent in plan.intents
    )


def build_decision(plan: ReusePlan) -> ReuseDecision:
    return ReuseDecision(
        plan.scope,
        ReuseAction.BUILD,
        "No suitable candidate was recorded; implement the narrow contract using existing stdlib.",
    )


def assessment_with_candidate(plan: ReusePlan, candidate: ReuseCandidate) -> ReuseAssessment:
    searches = list(completed_searches(plan))
    searches[2] = replace(searches[2], candidates=(candidate,))
    decision = ReuseDecision(
        plan.scope,
        ReuseAction.ADAPT,
        "Adapt the existing validation workflow to the new schema; retain its error conventions.",
        (candidate.candidate_id,),
    )
    return review_reuse_plan(plan, searches, decision)


def test_plan_is_task_specific_bounded_and_carries_internal_scope(plan: ReusePlan) -> None:
    assert {intent.channel for intent in plan.intents} == set(SearchChannel)
    assert len(plan.intents) == 3
    for intent in plan.intents:
        assert "JSON schema validation" in intent.query
        assert "Python 3.12" in intent.query
        assert "existing stdlib dataclasses" in intent.query
        assert intent.scope == plan.scope
        assert intent.max_results == 5
        assert str(plan.scope.workspace_id) not in intent.query
        assert str(plan.scope.project_id) not in intent.query
    assert "example code" in plan.intents[0].query
    assert "command line" in plan.intents[1].query
    assert "successful prior tasks" in plan.intents[2].query
    different_task = plan_reuse_search(plan.scope, "Compile a Rust CLI", ("Rust",))
    assert different_task.intents != plan.intents


def test_adapt_review_retains_scope_sources_and_decision_through_json(
    plan: ReusePlan, candidate: ReuseCandidate
) -> None:
    assessment = assessment_with_candidate(plan, candidate)
    restored = ReuseAssessment.from_json(assessment.to_json())
    assert restored == assessment
    assert restored.status is ReviewStatus.STRUCTURALLY_READY
    assert restored.degraded_channels == ()
    assert restored.decision.candidate_ids == (candidate.candidate_id,)
    assert restored.searches[2].candidates[0].source_ref == candidate.source_ref
    assert restored.plan.scope == plan.scope


def test_completed_search_with_no_candidates_allows_an_explicit_build(plan: ReusePlan) -> None:
    assessment = review_reuse_plan(plan, completed_searches(plan), build_decision(plan))
    assert assessment.status is ReviewStatus.STRUCTURALLY_READY
    assert assessment.decision.action is ReuseAction.BUILD
    assert all(search.status is SearchStatus.COMPLETED for search in assessment.searches)


@pytest.mark.parametrize("missing_index", range(3))
def test_missing_any_discovery_channel_blocks_review(plan: ReusePlan, missing_index: int) -> None:
    searches = completed_searches(plan)
    with pytest.raises(InvalidInputError, match="incomplete") as failure:
        review_reuse_plan(
            plan, searches[:missing_index] + searches[missing_index + 1 :], build_decision(plan)
        )
    assert searches[missing_index].intent.channel.value in str(failure.value)


def test_no_evidence_or_no_decision_cannot_silently_authorize_build(plan: ReusePlan) -> None:
    with pytest.raises(InvalidInputError, match="incomplete"):
        review_reuse_plan(plan, (), build_decision(plan))
    with pytest.raises(InvalidInputError, match="explicit decision"):
        review_reuse_plan(plan, completed_searches(plan), None)  # type: ignore[arg-type]


@pytest.mark.parametrize("status", [SearchStatus.UNAVAILABLE, SearchStatus.BUDGET_LIMITED])
def test_limited_search_remains_degraded_with_reason_and_attempt_reference(
    plan: ReusePlan, status: SearchStatus
) -> None:
    searches = list(completed_searches(plan))
    searches[0] = replace(
        searches[0],
        status=status,
        reason="The approved external search quota is unavailable for this task.",
        evidence_ref="attempt://quota-check/42",
    )
    decision = ReuseDecision(
        plan.scope,
        ReuseAction.BUILD,
        "Proceed with a narrow implementation; external library discovery was limited, not conclusive.",
    )
    assessment = review_reuse_plan(plan, searches, decision)
    restored = ReuseAssessment.from_json(assessment.to_json())
    assert restored.status is ReviewStatus.DEGRADED
    assert restored.degraded_channels == (SearchChannel.EXTERNAL_LIBRARIES,)
    assert restored.searches[0].status is status
    assert restored.searches[0].reason == searches[0].reason
    assert restored.searches[0].evidence_ref == "attempt://quota-check/42"
    assert json.loads(restored.to_json())["status"] == "degraded"


def test_budget_limited_attempt_can_preserve_usable_partial_results(
    plan: ReusePlan, candidate: ReuseCandidate
) -> None:
    assessment = assessment_with_candidate(plan, candidate)
    searches = tuple(
        replace(
            search,
            status=SearchStatus.BUDGET_LIMITED,
            reason="Only one internal result fit the budget.",
        )
        if search.intent.channel is SearchChannel.INTERNAL_WORKFLOWS
        else search
        for search in assessment.searches
    )
    reviewed = review_reuse_plan(plan, searches, assessment.decision)
    assert reviewed.status is ReviewStatus.DEGRADED
    assert reviewed.decision.candidate_ids == (candidate.candidate_id,)
    assert reviewed.searches[2].candidates == (candidate,)


@pytest.mark.parametrize("action", list(ReuseAction))
def test_candidate_references_must_exist_even_for_build(
    plan: ReusePlan, action: ReuseAction
) -> None:
    decision = ReuseDecision(plan.scope, action, "Use the documented approach.", (uuid4(),))
    with pytest.raises(InvalidInputError, match="absent from search evidence"):
        review_reuse_plan(plan, completed_searches(plan), decision)


@pytest.mark.parametrize("action", [ReuseAction.REUSE, ReuseAction.ADAPT])
def test_reuse_and_adapt_need_at_least_one_selected_candidate(
    plan: ReusePlan, action: ReuseAction
) -> None:
    with pytest.raises(InvalidInputError, match="at least one candidate"):
        ReuseDecision(plan.scope, action, "Prefer the existing solution.")


@pytest.mark.parametrize("field", ["task_id", "project_id", "workspace_id"])
def test_decision_cannot_cross_task_or_scope_boundaries(plan: ReusePlan, field: str) -> None:
    decision = replace(build_decision(plan), scope=replace(plan.scope, **{field: uuid4()}))
    with pytest.raises(InvalidInputError, match="decision task or scope"):
        review_reuse_plan(plan, completed_searches(plan), decision)


@pytest.mark.parametrize("field", ["task_id", "project_id", "workspace_id", "query", "max_results"])
def test_search_evidence_must_match_the_exact_planned_intent(plan: ReusePlan, field: str) -> None:
    searches = list(completed_searches(plan))
    intent = searches[0].intent
    if field in {"task_id", "project_id", "workspace_id"}:
        changed = replace(intent, scope=replace(plan.scope, **{field: uuid4()}))
    elif field == "query":
        changed = replace(intent, query="A search for an unrelated task")
    else:
        changed = replace(intent, max_results=6)
    searches[0] = replace(searches[0], intent=changed)
    with pytest.raises(InvalidInputError, match="differs from plan"):
        review_reuse_plan(plan, searches, build_decision(plan))


def test_duplicate_channel_does_not_count_as_complete_coverage(plan: ReusePlan) -> None:
    searches = completed_searches(plan)
    with pytest.raises(InvalidInputError, match="repeat a search channel"):
        review_reuse_plan(plan, (*searches, searches[0]), build_decision(plan))


def test_same_candidate_identifier_cannot_mean_different_sources(
    plan: ReusePlan, candidate: ReuseCandidate
) -> None:
    assessment = assessment_with_candidate(plan, candidate)
    searches = list(assessment.searches)
    searches[0] = replace(
        searches[0], candidates=(replace(candidate, source_ref="https://different.example/tool"),)
    )
    with pytest.raises(InvalidInputError, match="conflicting evidence"):
        review_reuse_plan(plan, searches, assessment.decision)


def test_candidate_found_by_two_channels_can_share_the_same_identity(
    plan: ReusePlan, candidate: ReuseCandidate
) -> None:
    assessment = assessment_with_candidate(plan, candidate)
    searches = list(assessment.searches)
    searches[0] = replace(searches[0], candidates=(candidate,))
    assert (
        review_reuse_plan(plan, searches, assessment.decision).status
        is ReviewStatus.STRUCTURALLY_READY
    )


@pytest.mark.parametrize("status", [SearchStatus.UNAVAILABLE, SearchStatus.BUDGET_LIMITED])
@pytest.mark.parametrize("reason", [None, "", " ", "x" * 1001])
def test_degraded_search_requires_a_concise_explicit_reason(
    plan: ReusePlan, status: SearchStatus, reason: str | None
) -> None:
    with pytest.raises(InvalidInputError):
        SearchEvidence(plan.intents[0], status, "attempt://failed/1", reason=reason)


def test_unavailable_cannot_claim_results_and_completed_cannot_hide_a_limit(
    plan: ReusePlan, candidate: ReuseCandidate
) -> None:
    with pytest.raises(InvalidInputError, match="cannot claim discovered candidates"):
        SearchEvidence(
            plan.intents[0], SearchStatus.UNAVAILABLE, "attempt://failed/1", (candidate,), "Offline"
        )
    with pytest.raises(InvalidInputError, match="limitation reason"):
        SearchEvidence(
            plan.intents[0], SearchStatus.COMPLETED, "attempt://limited/1", reason="Offline"
        )


def test_attempt_and_candidate_provenance_references_cannot_be_blank(
    plan: ReusePlan, candidate: ReuseCandidate
) -> None:
    with pytest.raises(InvalidInputError, match="evidence_ref"):
        SearchEvidence(plan.intents[0], SearchStatus.COMPLETED, " ")
    with pytest.raises(InvalidInputError, match="source_ref"):
        replace(candidate, source_ref="")


@pytest.mark.parametrize("rationale", ["", " ", "x" * 1001])
def test_explicit_decision_requires_concise_rationale(plan: ReusePlan, rationale: str) -> None:
    with pytest.raises(InvalidInputError, match="rationale"):
        ReuseDecision(plan.scope, ReuseAction.BUILD, rationale)


@pytest.mark.parametrize("limit", [0, 21, True, 1.5])
def test_query_result_bounds_are_validated(plan: ReusePlan, limit: object) -> None:
    with pytest.raises(InvalidInputError, match="max_results"):
        plan_reuse_search(plan.scope, plan.task_summary, max_results_per_channel=limit)  # type: ignore[arg-type]


def test_search_cannot_report_more_candidates_than_planned(
    plan: ReusePlan, candidate: ReuseCandidate
) -> None:
    intent = replace(plan.intents[0], max_results=1)
    with pytest.raises(InvalidInputError, match="exceeds"):
        SearchEvidence(
            intent,
            SearchStatus.COMPLETED,
            "attempt://search/1",
            (candidate, replace(candidate, candidate_id=uuid4())),
        )


def test_all_record_types_round_trip_and_reject_unknown_schema_versions(
    plan: ReusePlan, candidate: ReuseCandidate
) -> None:
    assessment = assessment_with_candidate(plan, candidate)
    records = (
        plan.scope,
        plan.intents[0],
        plan,
        candidate,
        assessment.searches[2],
        assessment.decision,
        assessment,
    )
    for record in records:
        assert type(record).from_json(record.to_json()) == record
        for version in (2, True, "1", 1.0):
            payload = record.to_dict()
            payload["schema_version"] = version
            with pytest.raises(InvalidInputError, match="schema_version"):
                type(record).from_dict(payload)


def test_nested_unknown_version_and_forged_review_status_are_rejected(
    plan: ReusePlan, candidate: ReuseCandidate
) -> None:
    assessment = assessment_with_candidate(plan, candidate)
    payload = assessment.to_dict()
    payload["searches"][2]["candidates"][0]["schema_version"] = 99
    with pytest.raises(InvalidInputError, match="schema_version"):
        ReuseAssessment.from_dict(payload)
    payload = assessment.to_dict()
    payload["status"] = "degraded"
    with pytest.raises(InvalidInputError, match="contradicts"):
        ReuseAssessment.from_dict(payload)


def test_unknown_states_and_extra_fields_cannot_bypass_contract(plan: ReusePlan) -> None:
    payload = completed_searches(plan)[0].to_dict()
    payload["status"] = "pending"
    with pytest.raises(InvalidInputError, match="SearchStatus"):
        SearchEvidence.from_dict(payload)
    payload = build_decision(plan).to_dict()
    payload["action"] = "skip_research"
    with pytest.raises(InvalidInputError, match="ReuseAction"):
        ReuseDecision.from_dict(payload)
    payload = plan.to_dict()
    payload["approved"] = True
    with pytest.raises(InvalidInputError, match="record fields"):
        ReusePlan.from_dict(payload)


def test_records_and_collection_fields_remain_immutable(plan: ReusePlan) -> None:
    with pytest.raises(FrozenInstanceError):
        plan.task_summary = "A different task"  # type: ignore[misc]
    with pytest.raises(InvalidInputError, match="immutable tuple"):
        replace(plan, intents=list(plan.intents))
    payload = plan.to_dict()
    payload["intents"] = {}
    with pytest.raises(InvalidInputError, match="JSON array"):
        ReusePlan.from_dict(payload)


def test_invalid_task_context_and_duplicate_plan_channels_reject(plan: ReusePlan) -> None:
    with pytest.raises(InvalidInputError, match="task_summary"):
        plan_reuse_search(plan.scope, " ")
    with pytest.raises(InvalidInputError, match="context_terms"):
        plan_reuse_search(plan.scope, plan.task_summary, (" ",))
    with pytest.raises(InvalidInputError, match="exactly one intent"):
        replace(plan, intents=(plan.intents[0], plan.intents[0], plan.intents[2]))
    with pytest.raises(InvalidInputError, match="scope differs"):
        replace(plan, scope=replace(plan.scope, project_id=uuid4()))
