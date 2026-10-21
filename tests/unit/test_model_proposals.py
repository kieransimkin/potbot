"""Model declarations cannot manufacture controller identities or capabilities."""

from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.controller.information_needs import NeedSpec
from potbot.domain.coding import ScriptedCodingResponse
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.execution import ActionProposal, ToolDefinition, ToolRegistry
from potbot.domain.model_proposals import (
    ControllerIdentity,
    ModelActionFrame,
    ModelActionProposal,
    ModelCodingResponse,
    Predicate,
    PredicateResult,
    ProposalAuthority,
    action_registry_subset,
    admit_action_frame,
    admit_coding_response,
    evaluate_predicate,
)


def identity() -> ControllerIdentity:
    return ControllerIdentity(*(uuid4() for _ in range(6)))


def need(**changes: object) -> NeedSpec:
    value = NeedSpec(
        question="Which existing workflow already produces this artifact?",
        pending_decision="Reuse an installed workflow or implement the missing behavior.",
        alternatives=("Reuse", "Adapt", "Build"),
        required_evidence=("A workflow contract and a verified example output",),
        resolution_criteria=("The result satisfies the requested input and output formats",),
        scope="This repository and the permitted internal workflow catalogue",
    )
    return replace(value, **changes)


def proposal(name: str = "inspect", **changes: object) -> ModelActionProposal:
    value = ModelActionProposal(
        "first",
        ActionProposal(name),
        "Determine whether a dependency can be reused.",
        ("A bounded result with a stable evidence reference",),
    )
    return replace(value, **changes)


def registry(*names: str, arguments: bool = False) -> ToolRegistry:
    return ToolRegistry(
        tuple(
            ToolDefinition(name, "inspect", ("never-executed",), allow_arguments=arguments)
            for name in names
        )
    )


@pytest.mark.parametrize(
    "kind",
    [
        "inspect",
        "memory_lookup",
        "external_search",
        "example_lookup",
        "experiment",
        "implementation",
    ],
)
def test_only_installed_relevant_actions_are_admitted(kind: str) -> None:
    authority = ProposalAuthority(identity(), (kind,))
    frame = ModelActionFrame(authority.identity, (proposal(kind),))
    restored = ModelActionFrame.from_dict(frame.to_dict())
    result = admit_action_frame(restored, registry(kind), authority, {})
    assert result.status == "admitted"
    assert result.proposals[0].action == ActionProposal(kind)
    assert result.proposals[0].action.to_dict() == frame.actions[0].action.to_dict()


def test_model_frame_alone_grants_no_authority() -> None:
    authority = ProposalAuthority(identity(), ("inspect",))
    frame = ModelActionFrame(authority.identity, (proposal("invented"),))
    # Structural parsing intentionally does not claim semantic admission.
    assert ModelActionFrame.from_dict(frame.to_dict()) == frame
    with pytest.raises(InvalidInputError, match="Unknown registered"):
        admit_action_frame(frame, registry("inspect"), authority, {})
    with pytest.raises(ScopeDeniedError, match="rendered inventory"):
        admit_action_frame(frame, registry("invented"), authority, {})


@pytest.mark.parametrize("field", list(ControllerIdentity.__dataclass_fields__))
def test_every_controller_identity_echo_is_checked(field: str) -> None:
    authority = ProposalAuthority(identity(), ("inspect",))
    frame = ModelActionFrame(replace(authority.identity, **{field: uuid4()}), (proposal(),))
    with pytest.raises(StateMismatchError):
        admit_action_frame(frame, registry("inspect"), authority, {})


def test_installed_argument_policy_is_still_enforced() -> None:
    authority = ProposalAuthority(identity(), ("inspect",))
    frame = ModelActionFrame(
        authority.identity, (proposal(action=ActionProposal("inspect", ("--unsafe",))),)
    )
    with pytest.raises(ScopeDeniedError):
        admit_action_frame(frame, registry("inspect"), authority, {})
    assert (
        admit_action_frame(frame, registry("inspect", arguments=True), authority, {}).status
        == "admitted"
    )


@pytest.mark.parametrize(
    "field,authority_field",
    [
        ("evidence_ids", "evidence_ids"),
        ("information_need_ids", "information_need_ids"),
        ("prior_dependencies", "dependency_ids"),
        ("criterion_ids", "criterion_ids"),
    ],
)
def test_references_require_independently_supplied_inventory(
    field: str, authority_field: str
) -> None:
    reference = "R1" if field == "criterion_ids" else uuid4()
    authority = ProposalAuthority(identity(), ("inspect",))
    frame = ModelActionFrame(authority.identity, (proposal(**{field: (reference,)}),))
    with pytest.raises(ScopeDeniedError):
        admit_action_frame(frame, registry("inspect"), authority, {})
    allowed = replace(authority, **{authority_field: (reference,)})
    assert admit_action_frame(frame, registry("inspect"), allowed, {}).status == "admitted"


def test_model_cannot_create_resource_classes() -> None:
    authority = ProposalAuthority(identity(), ("inspect",))
    frame = ModelActionFrame(authority.identity, (proposal(resource_class="unlimited"),))
    with pytest.raises(ScopeDeniedError, match="resource class"):
        admit_action_frame(frame, registry("inspect"), authority, {})


def test_question_only_response_does_not_fabricate_an_action() -> None:
    authority = ProposalAuthority(identity(), ())
    frame = ModelActionFrame(authority.identity, information_needs=(need(),))
    result = admit_action_frame(
        ModelActionFrame.from_dict(frame.to_dict()), registry(), authority, {}
    )
    assert result.status == "admitted"
    assert result.proposals == ()


@pytest.mark.parametrize(
    "alteration", [{"known_evidence": (uuid4(),)}, {"query_profiles": ("invented_profile",)}]
)
def test_questions_do_not_expand_evidence_or_retrieval_authority(alteration: dict) -> None:
    authority = ProposalAuthority(identity(), ())
    frame = ModelActionFrame(authority.identity, information_needs=(need(**alteration),))
    with pytest.raises(ScopeDeniedError):
        admit_action_frame(frame, registry(), authority, {})


def test_dependency_graph_is_bounded_acyclic_and_preserved() -> None:
    first = proposal()
    second = proposal(proposal_id="second", dependencies=("first",))
    frame = ModelActionFrame(identity(), (second, first))
    assert ModelActionFrame.from_dict(frame.to_dict()).actions[0].dependencies == ("first",)
    for actions in (
        (first, first),
        (replace(first, dependencies=("absent",)),),
        (replace(first, dependencies=("first",)),),
        (replace(first, dependencies=("second",)), second),
    ):
        with pytest.raises(InvalidInputError):
            ModelActionFrame(identity(), actions)
    with pytest.raises(InvalidInputError):
        ModelActionFrame(identity(), tuple(replace(first, proposal_id=f"p{i}") for i in range(9)))


@pytest.mark.parametrize(
    "alteration",
    [
        {"execute_now": True},
        {"schema_version": True},
        {"response_schema": "anything"},
        {"identity": {}},
        {"actions": "inspect"},
    ],
)
def test_malformed_frames_are_rejected(alteration: dict) -> None:
    raw = ModelActionFrame(identity(), (proposal(),)).to_dict()
    raw.update(alteration)
    with pytest.raises(InvalidInputError):
        ModelActionFrame.from_dict(raw)


def test_action_cannot_supply_controller_permissions() -> None:
    raw = ModelActionFrame(identity(), (proposal(),)).to_dict()
    raw["actions"][0]["action"]["allowed_secret_refs"] = ["secret://anything"]
    with pytest.raises(InvalidInputError):
        ModelActionFrame.from_dict(raw)


@pytest.mark.parametrize(
    "predicate,facts,expected",
    [
        (Predicate("eq", "task.status", "running"), {"task.status": "running"}, "satisfied"),
        (Predicate("ne", "task.status", "running"), {"task.status": "running"}, "unsatisfied"),
        (Predicate("eq", "task.cancelled", False), {"task.cancelled": False}, "satisfied"),
        (Predicate("eq", "task.cancelled", 0), {"task.cancelled": False}, "unknown"),
        (Predicate("eq", "task.cancelled", False), {"task.cancelled": 0}, "unknown"),
        (
            Predicate("in", "task.status", ("running", "queued")),
            {"task.status": "running"},
            "satisfied",
        ),
        (Predicate("exists", "environment.id"), {"environment.id": "known"}, "satisfied"),
        (Predicate("exists", "environment.id"), {}, "unknown"),
        (Predicate("eq", "unknown.field", True), {"unknown.field": True}, "unknown"),
    ],
)
def test_predicates_use_explicit_typed_facts(
    predicate: Predicate, facts: dict, expected: str
) -> None:
    assert evaluate_predicate(Predicate.from_dict(predicate.to_dict()), facts) == expected


def test_missing_fact_stays_unknown_under_negation() -> None:
    missing = Predicate("eq", "repository.clean", True)
    assert evaluate_predicate(Predicate("not", operands=(missing,)), {}) is PredicateResult.UNKNOWN
    false = Predicate("eq", "task.status", "stopped")
    true = Predicate("eq", "task.status", "running")
    facts = {"task.status": "running"}
    assert (
        evaluate_predicate(Predicate("all", operands=(missing, false)), facts)
        is PredicateResult.UNSATISFIED
    )
    assert (
        evaluate_predicate(Predicate("all", operands=(missing, true)), facts)
        is PredicateResult.UNKNOWN
    )
    assert (
        evaluate_predicate(Predicate("any", operands=(missing, true)), facts)
        is PredicateResult.SATISFIED
    )
    assert (
        evaluate_predicate(Predicate("any", operands=(missing, false)), facts)
        is PredicateResult.UNKNOWN
    )


@pytest.mark.parametrize(
    "raw",
    [
        {"op": "eval", "field": "task.status", "value": "__import__('os')"},
        {"op": "eq", "field": "task.__class__.mro", "value": "x"},
        {"op": "exists", "field": "task.status", "value": True},
        {"op": "all", "operands": []},
        {"op": "not", "operands": []},
        {"op": "in", "field": "task.status", "value": "running"},
        {"op": "eq", "field": "task.status", "value": {}},
        {"op": "eq", "field": "task.status", "value": float("nan")},
    ],
)
def test_predicates_reject_expressions_and_invalid_values(raw: dict) -> None:
    with pytest.raises(InvalidInputError):
        Predicate.from_dict(raw)


def test_predicate_depth_and_total_nodes_are_bounded() -> None:
    raw = {"op": "eq", "field": "task.status", "value": "running"}
    for _ in range(8):
        raw = {"op": "not", "operands": [raw]}
    with pytest.raises(InvalidInputError, match="nesting"):
        Predicate.from_dict(raw)
    leaf = Predicate("exists", "task.status")
    broad = Predicate("all", operands=(leaf,) * 16)
    with pytest.raises(InvalidInputError, match="node limit"):
        Predicate("all", operands=(broad,) * 4)


@pytest.mark.parametrize(
    "preconditions,stops,facts,decision",
    [
        ((Predicate("eq", "task.status", "running"),), (), {"task.status": "stopped"}, "blocked"),
        ((Predicate("exists", "repository.state_id"),), (), {}, "unknown"),
        ((), (Predicate("eq", "task.cancelled", True),), {"task.cancelled": True}, "stopped"),
        ((), (Predicate("eq", "task.cancelled", True),), {}, "unknown"),
    ],
)
def test_blocked_frame_never_exposes_partial_executable_actions(
    preconditions: tuple, stops: tuple, facts: dict, decision: str
) -> None:
    authority = ProposalAuthority(identity(), ("inspect",))
    first = proposal(preconditions=preconditions, stop_conditions=stops)
    second = proposal(proposal_id="second", dependencies=("first",))
    result = admit_action_frame(
        ModelActionFrame(authority.identity, (first, second)), registry("inspect"), authority, facts
    )
    assert result.status == "blocked"
    assert result.proposals == ()
    assert result.decisions[0].status == decision


def test_rendered_action_subset_does_not_disclose_executables_or_credentials() -> None:
    installed = ToolRegistry(
        (
            ToolDefinition(
                "inspect",
                "inspect",
                ("/private/path",),
                environment=(("TOKEN", "not-a-real-secret"),),
                secret_refs=(("CREDENTIAL", "secret://provider"),),
            ),
        )
    )
    subset = action_registry_subset(installed, ("inspect",))
    assert subset[0]["kind"] == "inspect"
    assert subset[0]["definition_digest"] == installed.get("inspect").fingerprint
    assert all(
        text not in str(subset)
        for text in ("/private/path", "not-a-real-secret", "secret://provider")
    )


def coding(**changes: object) -> ModelCodingResponse:
    value = ModelCodingResponse(
        uuid4(),
        uuid4(),
        uuid4(),
        patch=b"--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-old\n+new\n",
        changed_files=("src/a.py",),
        proposed_checks=("unit",),
    )
    return replace(value, **changes)


def coding_authority(response: ModelCodingResponse) -> dict:
    return {
        "implementation_id": response.implementation_id,
        "base_state_id": response.base_state_id,
        "contract_revision_id": response.contract_revision_id,
        "permitted_paths": ("src",),
        "known_checks": ("unit",),
    }


def test_coding_response_preserves_existing_value_contract_and_model_origin() -> None:
    response = coding()
    restored = ModelCodingResponse.from_dict(response.to_dict())
    assert restored == response
    assert isinstance(response.as_scripted_response(), ScriptedCodingResponse)
    assert admit_coding_response(response, **coding_authority(response)) is response
    artifact_id = uuid4()
    result = response.to_result_dict(artifact_id)
    assert result["producer_kind"] == "model"
    assert result["patch_artifact_id"] == str(artifact_id)
    assert result["outcome"] == "patch"
    assert "patch" not in result


@pytest.mark.parametrize("field", ["implementation_id", "base_state_id", "contract_revision_id"])
def test_coder_cannot_change_assignment_or_base(field: str) -> None:
    response = coding()
    authority = coding_authority(response)
    authority[field] = uuid4()
    with pytest.raises(StateMismatchError):
        admit_coding_response(response, **authority)


@pytest.mark.parametrize(
    "changes",
    [
        {"changed_files": ("outside.py",)},
        {"proposed_checks": ("run-anything",)},
        {"evidence_ids": (uuid4(),)},
    ],
)
def test_coder_references_and_declared_scope_are_bounded(changes: dict) -> None:
    response = coding(**changes)
    with pytest.raises(ScopeDeniedError):
        admit_coding_response(response, **coding_authority(response))


def test_coding_question_is_an_inability_without_fabricated_patch() -> None:
    response = coding(
        patch=None,
        changed_files=(),
        inability_code="insufficient_context",
        inability_reason="The workflow contract is absent.",
        information_needs=(need(),),
    )
    restored = ModelCodingResponse.from_dict(response.to_dict())
    assert restored.patch is None
    assert len(restored.information_needs) == 1
    assert restored.to_result_dict()["outcome"] == "inability"
    with pytest.raises(InvalidInputError):
        replace(response, patch=b"invented", changed_files=("src/a.py",))


@pytest.mark.parametrize(
    "changes",
    [
        {"changed_files": ()},
        {"changed_files": ("../escape.py",)},
        {"changed_files": (".git/config",)},
        {"proposed_checks": ("unit", "unit")},
        {"patch": b"\xff"},
        {"patch": b"\x00"},
    ],
)
def test_coding_output_rejects_invalid_declarations(changes: dict) -> None:
    with pytest.raises((InvalidInputError, ScopeDeniedError)):
        coding(**changes)


def test_result_requires_patch_artifact_only_for_patch_outcome() -> None:
    with pytest.raises(InvalidInputError):
        coding().to_result_dict()
    inability = coding(
        patch=None,
        changed_files=(),
        inability_code="unsupported",
        inability_reason="Required compiler is unavailable.",
    )
    with pytest.raises(InvalidInputError):
        inability.to_result_dict(uuid4())
