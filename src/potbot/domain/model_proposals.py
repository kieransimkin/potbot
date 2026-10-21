"""Untrusted model frames above the existing action and coding contracts.

These records never dispatch tools, change authorization, or publish verification.
Controller echoes and references are checked against an independently supplied
authority. The durable ActionProposal representation remains unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from potbot.controller.information_needs import NeedSpec
from potbot.domain.coding import (
    CODING_RESPONSE_SCHEMA,
    ScriptedCodingResponse,
    coding_path,
    within_scope,
)
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.execution import ActionProposal, ToolRegistry, bounded_strings

ACTION_RESPONSE_SCHEMA = "action-proposal-v1"
MAX_ACTIONS = 8
MAX_INFORMATION_NEEDS = 8
MAX_PREDICATE_DEPTH = 8
MAX_PREDICATE_NODES = 64
Scalar = str | int | bool | None


def _object(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise InvalidInputError(f"{label} fields do not match the declared schema.")
    return value


def _version(value: object) -> None:
    if type(value) is not int or value != 1:
        raise InvalidInputError("Unsupported model frame schema version.")


def _text(value: object, label: str, *, limit: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise InvalidInputError(f"{label} must be bounded nonempty text.")
    return value


def _uuid(value: object, label: str) -> UUID:
    if not isinstance(value, str):
        raise InvalidInputError(f"{label} must be a UUID string.")
    try:
        identity = UUID(value)
    except ValueError:
        raise InvalidInputError(f"{label} must be a UUID string.") from None
    if str(identity) != value:
        raise InvalidInputError(f"{label} must use canonical UUID spelling.")
    return identity


def _uuids(value: object, label: str, *, limit: int = 128) -> tuple[UUID, ...]:
    if not isinstance(value, tuple) or len(value) > limit:
        raise InvalidInputError(f"{label} must be a bounded immutable UUID sequence.")
    if any(not isinstance(identity, UUID) for identity in value) or len(set(value)) != len(value):
        raise InvalidInputError(f"{label} must contain distinct UUIDs.")
    return value


def _array(value: object, label: str, *, limit: int = 128) -> list[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise InvalidInputError(f"{label} must be a bounded array.")
    return value


def _uuid_array(value: object, label: str) -> tuple[UUID, ...]:
    return _uuids(tuple(_uuid(item, label) for item in _array(value, label)), label)


def _strings(value: object, label: str, *, limit: int = 128) -> tuple[str, ...]:
    result = bounded_strings(value, label, limit=limit)
    if len(set(result)) != len(result):
        raise InvalidInputError(f"{label} cannot repeat values.")
    return result


def _string_array(value: object, label: str, *, limit: int = 128) -> tuple[str, ...]:
    return _strings(tuple(_array(value, label, limit=limit)), label, limit=limit)


def need_from_dict(value: object) -> NeedSpec:
    """Decode the existing need contract; authority is checked separately."""
    item = _object(
        value,
        {
            "schema_version",
            "question",
            "pending_decision",
            "alternatives",
            "required_evidence",
            "resolution_criteria",
            "scope",
            "known_evidence",
            "priority",
            "budget",
            "query_profiles",
        },
        "Information need",
    )
    _version(item["schema_version"])
    if not isinstance(item["budget"], dict) or len(item["budget"]) > 16:
        raise InvalidInputError("Need budget must be a bounded object.")
    for field in ("question", "pending_decision", "scope"):
        _text(item[field], field, limit=8000)
    return NeedSpec(
        question=item["question"],
        pending_decision=item["pending_decision"],
        alternatives=_string_array(item["alternatives"], "alternatives"),
        required_evidence=_string_array(item["required_evidence"], "required_evidence"),
        resolution_criteria=_string_array(item["resolution_criteria"], "resolution_criteria"),
        scope=item["scope"],
        known_evidence=_uuid_array(item["known_evidence"], "known_evidence"),
        priority=item["priority"],
        budget=tuple(item["budget"].items()),
        query_profiles=_string_array(item["query_profiles"], "query_profiles"),
    )


def _needs(value: object) -> tuple[NeedSpec, ...]:
    if (
        not isinstance(value, tuple)
        or len(value) > MAX_INFORMATION_NEEDS
        or any(not isinstance(need, NeedSpec) for need in value)
    ):
        raise InvalidInputError("Information needs must be a bounded typed sequence.")
    return value


@dataclass(frozen=True, slots=True)
class ControllerIdentity:
    episode_id: UUID
    contract_revision_id: UUID
    knowledge_view_id: UUID
    repository_state_id: UUID
    environment_id: UUID
    node_id: UUID

    def __post_init__(self) -> None:
        if any(not isinstance(getattr(self, field), UUID) for field in self.__dataclass_fields__):
            raise InvalidInputError("Controller identity requires exact UUID echoes.")

    def to_dict(self) -> dict[str, str]:
        return {field: str(getattr(self, field)) for field in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: object) -> ControllerIdentity:
        item = _object(value, set(cls.__dataclass_fields__), "Controller identity")
        return cls(**{field: _uuid(raw, field) for field, raw in item.items()})


class PredicateResult(StrEnum):
    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
    UNKNOWN = "unknown"


# Flat, explicitly named controller facts; no dynamic object/member traversal.
PREDICATE_FIELDS: dict[str, type[str] | type[bool] | type[int]] = {
    "task.status": str,
    "task.contract_current": bool,
    "task.cancelled": bool,
    "task.active_contract_id": str,
    "repository.state_id": str,
    "repository.revision": str,
    "repository.clean": bool,
    "environment.id": str,
    "environment.available": bool,
    "environment.fingerprint": str,
    "environment.network_allowed": bool,
}
PREDICATE_OPERATORS = frozenset({"all", "any", "not", "eq", "ne", "in", "exists"})


@dataclass(frozen=True, slots=True)
class Predicate:
    op: str
    field: str | None = None
    value: Scalar | tuple[Scalar, ...] = None
    operands: tuple[Predicate, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.op, str) or self.op not in PREDICATE_OPERATORS:
            raise InvalidInputError("Unknown predicate operator.")
        if not isinstance(self.operands, tuple) or any(
            not isinstance(child, Predicate) for child in self.operands
        ):
            raise InvalidInputError("Predicate operands must be immutable predicates.")
        if self.op in {"all", "any", "not"}:
            if (
                self.field is not None
                or self.value is not None
                or not 1 <= len(self.operands) <= 16
                or (self.op == "not" and len(self.operands) != 1)
            ):
                raise InvalidInputError("Logical predicate has invalid operands.")
        else:
            if (
                not isinstance(self.field, str)
                or not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]{0,79}", self.field)
                or self.operands
            ):
                raise InvalidInputError("Predicate must name one bounded controller field.")
            if self.op == "exists":
                if self.value is not None:
                    raise InvalidInputError("Existence predicate cannot have a comparison value.")
            elif self.op == "in":
                if not isinstance(self.value, tuple) or not 1 <= len(self.value) <= 32:
                    raise InvalidInputError("Membership predicate requires bounded alternatives.")
                for value in self.value:
                    _scalar(value)
            else:
                _scalar(self.value)
        _predicate_size(self)

    def to_dict(self) -> dict[str, Any]:
        if self.op in {"all", "any", "not"}:
            return {"op": self.op, "operands": [child.to_dict() for child in self.operands]}
        if self.op == "exists":
            return {"op": self.op, "field": self.field}
        return {
            "op": self.op,
            "field": self.field,
            "value": list(self.value) if isinstance(self.value, tuple) else self.value,
        }

    @classmethod
    def from_dict(cls, value: object, *, _depth: int = 1) -> Predicate:
        if _depth > MAX_PREDICATE_DEPTH:
            raise InvalidInputError("Predicate exceeds the nesting limit.")
        if not isinstance(value, dict) or not isinstance(value.get("op"), str):
            raise InvalidInputError("A predicate must be a typed object.")
        op = value["op"]
        if op in {"all", "any", "not"}:
            item = _object(value, {"op", "operands"}, "Logical predicate")
            return cls(
                op,
                operands=tuple(
                    cls.from_dict(child, _depth=_depth + 1)
                    for child in _array(item["operands"], "operands", limit=16)
                ),
            )
        if op == "exists":
            item = _object(value, {"op", "field"}, "Existence predicate")
            return cls(op, field=item["field"])
        item = _object(value, {"op", "field", "value"}, "Comparison predicate")
        comparison = item["value"]
        if op == "in":
            comparison = tuple(_array(comparison, "predicate alternatives", limit=32))
        return cls(op, field=item["field"], value=comparison)


def _scalar(value: object) -> None:
    if type(value) not in (str, int, bool, type(None)):
        raise InvalidInputError("Predicate values must be finite bounded JSON scalars.")
    if isinstance(value, str) and (len(value) > 4096 or "\x00" in value):
        raise InvalidInputError("Predicate comparison text exceeds its bound.")
    if type(value) is int and not -(2**63) <= value < 2**63:
        raise InvalidInputError("Predicate integer exceeds its bound.")


def _predicate_size(root: Predicate) -> int:
    count = 0
    stack = [(root, 1)]
    while stack:
        predicate, depth = stack.pop()
        count += 1
        if count > MAX_PREDICATE_NODES or depth > MAX_PREDICATE_DEPTH:
            raise InvalidInputError("Predicate exceeds its depth or node limit.")
        stack.extend((child, depth + 1) for child in predicate.operands)
    return count


def evaluate_predicate(predicate: Predicate, facts: Mapping[str, object]) -> PredicateResult:
    """Evaluate only registered flat facts using three-valued logic."""
    if not isinstance(predicate, Predicate):
        raise InvalidInputError("A typed predicate is required.")
    if predicate.op in {"all", "any", "not"}:
        values = tuple(evaluate_predicate(child, facts) for child in predicate.operands)
        if predicate.op == "not":
            return {
                PredicateResult.SATISFIED: PredicateResult.UNSATISFIED,
                PredicateResult.UNSATISFIED: PredicateResult.SATISFIED,
                PredicateResult.UNKNOWN: PredicateResult.UNKNOWN,
            }[values[0]]
        if predicate.op == "all":
            if PredicateResult.UNSATISFIED in values:
                return PredicateResult.UNSATISFIED
            return (
                PredicateResult.UNKNOWN
                if PredicateResult.UNKNOWN in values
                else PredicateResult.SATISFIED
            )
        if PredicateResult.SATISFIED in values:
            return PredicateResult.SATISFIED
        return (
            PredicateResult.UNKNOWN
            if PredicateResult.UNKNOWN in values
            else PredicateResult.UNSATISFIED
        )
    field = predicate.field
    if field is None or field not in PREDICATE_FIELDS or field not in facts:
        return PredicateResult.UNKNOWN
    actual = facts[field]
    if type(actual) is not PREDICATE_FIELDS[field]:
        return PredicateResult.UNKNOWN
    if predicate.op == "exists":
        return PredicateResult.SATISFIED
    if predicate.op == "in":
        assert isinstance(predicate.value, tuple)
        matched = any(type(actual) is type(value) and actual == value for value in predicate.value)
    else:
        if type(actual) is not type(predicate.value):
            return PredicateResult.UNKNOWN
        matched = actual == predicate.value
        if predicate.op == "ne":
            matched = not matched
    return PredicateResult.SATISFIED if matched else PredicateResult.UNSATISFIED


def _predicates(value: object, label: str) -> tuple[Predicate, ...]:
    if (
        not isinstance(value, tuple)
        or len(value) > 16
        or any(not isinstance(item, Predicate) for item in value)
        or sum(_predicate_size(item) for item in value) > MAX_PREDICATE_NODES
    ):
        raise InvalidInputError(f"{label} must be a bounded predicate sequence.")
    return value


@dataclass(frozen=True, slots=True)
class ModelActionProposal:
    proposal_id: str
    action: ActionProposal
    purpose: str
    expected_observations: tuple[str, ...]
    criterion_ids: tuple[str, ...] = ()
    resource_class: str = "standard"
    evidence_ids: tuple[UUID, ...] = ()
    information_need_ids: tuple[UUID, ...] = ()
    prior_dependencies: tuple[UUID, ...] = ()
    dependencies: tuple[str, ...] = ()
    preconditions: tuple[Predicate, ...] = ()
    stop_conditions: tuple[Predicate, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.proposal_id, str) or not re.fullmatch(
            r"[a-z][a-z0-9_-]{0,39}", self.proposal_id
        ):
            raise InvalidInputError("Proposal identifier must be a bounded local name.")
        if not isinstance(self.action, ActionProposal):
            raise InvalidInputError("A model action must wrap the durable ActionProposal.")
        _text(self.purpose, "purpose")
        _text(self.resource_class, "resource_class", limit=80)
        _strings(self.expected_observations, "expected_observations", limit=16)
        if not self.expected_observations:
            raise InvalidInputError("Model actions must state expected observations.")
        _strings(self.criterion_ids, "criterion_ids")
        _strings(self.dependencies, "dependencies", limit=MAX_ACTIONS)
        for field in ("evidence_ids", "information_need_ids", "prior_dependencies"):
            _uuids(getattr(self, field), field)
        _predicates(self.preconditions, "preconditions")
        _predicates(self.stop_conditions, "stop_conditions")

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "action": self.action.to_dict(),
            "purpose": self.purpose,
            "expected_observations": list(self.expected_observations),
            "criterion_ids": list(self.criterion_ids),
            "resource_class": self.resource_class,
            "evidence_ids": list(map(str, self.evidence_ids)),
            "information_need_ids": list(map(str, self.information_need_ids)),
            "prior_dependencies": list(map(str, self.prior_dependencies)),
            "dependencies": list(self.dependencies),
            "preconditions": [item.to_dict() for item in self.preconditions],
            "stop_conditions": [item.to_dict() for item in self.stop_conditions],
        }

    @classmethod
    def from_dict(cls, value: object) -> ModelActionProposal:
        item = _object(value, set(cls.__dataclass_fields__), "Model action")
        return cls(
            proposal_id=item["proposal_id"],
            action=ActionProposal.from_dict(item["action"]),
            purpose=item["purpose"],
            expected_observations=_string_array(
                item["expected_observations"], "expected_observations", limit=16
            ),
            criterion_ids=_string_array(item["criterion_ids"], "criterion_ids"),
            resource_class=item["resource_class"],
            evidence_ids=_uuid_array(item["evidence_ids"], "evidence_ids"),
            information_need_ids=_uuid_array(item["information_need_ids"], "information_need_ids"),
            prior_dependencies=_uuid_array(item["prior_dependencies"], "prior_dependencies"),
            dependencies=_string_array(item["dependencies"], "dependencies", limit=MAX_ACTIONS),
            preconditions=tuple(
                Predicate.from_dict(raw)
                for raw in _array(item["preconditions"], "preconditions", limit=16)
            ),
            stop_conditions=tuple(
                Predicate.from_dict(raw)
                for raw in _array(item["stop_conditions"], "stop_conditions", limit=16)
            ),
        )


@dataclass(frozen=True, slots=True)
class ModelActionFrame:
    identity: ControllerIdentity
    actions: tuple[ModelActionProposal, ...] = ()
    information_needs: tuple[NeedSpec, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ControllerIdentity):
            raise InvalidInputError("Model frame requires controller identity echoes.")
        if (
            not isinstance(self.actions, tuple)
            or len(self.actions) > MAX_ACTIONS
            or any(not isinstance(action, ModelActionProposal) for action in self.actions)
        ):
            raise InvalidInputError("Model frame actions must be a bounded typed sequence.")
        _needs(self.information_needs)
        if not self.actions and not self.information_needs:
            raise InvalidInputError("A model frame must propose an action or focused question.")
        actions = {action.proposal_id: action for action in self.actions}
        if len(actions) != len(self.actions):
            raise InvalidInputError("Local proposal identifiers must be distinct.")
        for action in self.actions:
            if not set(action.dependencies).issubset(actions):
                raise InvalidInputError("Local dependency names an unknown proposal.")
        pending = {identity: set(action.dependencies) for identity, action in actions.items()}
        while pending:
            ready = {identity for identity, dependencies in pending.items() if not dependencies}
            if not ready:
                raise InvalidInputError("Local proposal dependencies contain a cycle.")
            pending = {
                identity: dependencies - ready
                for identity, dependencies in pending.items()
                if identity not in ready
            }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "response_schema": ACTION_RESPONSE_SCHEMA,
            "identity": self.identity.to_dict(),
            "actions": [action.to_dict() for action in self.actions],
            "information_needs": [need.to_dict() for need in self.information_needs],
        }

    @classmethod
    def from_dict(cls, value: object) -> ModelActionFrame:
        item = _object(
            value,
            {"schema_version", "response_schema", "identity", "actions", "information_needs"},
            "Model action frame",
        )
        _version(item["schema_version"])
        if item["response_schema"] != ACTION_RESPONSE_SCHEMA:
            raise InvalidInputError("Unexpected action response schema.")
        return cls(
            ControllerIdentity.from_dict(item["identity"]),
            tuple(
                ModelActionProposal.from_dict(raw)
                for raw in _array(item["actions"], "actions", limit=MAX_ACTIONS)
            ),
            tuple(
                need_from_dict(raw)
                for raw in _array(
                    item["information_needs"], "information_needs", limit=MAX_INFORMATION_NEEDS
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class ProposalAuthority:
    identity: ControllerIdentity
    allowed_tools: tuple[str, ...]
    evidence_ids: tuple[UUID, ...] = ()
    information_need_ids: tuple[UUID, ...] = ()
    criterion_ids: tuple[str, ...] = ()
    dependency_ids: tuple[UUID, ...] = ()
    resource_classes: tuple[str, ...] = ("standard",)
    query_profiles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ControllerIdentity):
            raise InvalidInputError("Controller authority requires a typed identity.")
        for field in ("allowed_tools", "criterion_ids", "resource_classes", "query_profiles"):
            _strings(getattr(self, field), field)
        for field in ("evidence_ids", "information_need_ids", "dependency_ids"):
            _uuids(getattr(self, field), field)


@dataclass(frozen=True, slots=True)
class ProposalDecision:
    proposal_id: str
    status: str
    preconditions: tuple[PredicateResult, ...]
    stop_conditions: tuple[PredicateResult, ...]


@dataclass(frozen=True, slots=True)
class ProposalAdmission:
    status: str
    proposals: tuple[ModelActionProposal, ...]
    decisions: tuple[ProposalDecision, ...]


def _check_needs(
    needs: tuple[NeedSpec, ...], evidence_ids: tuple[UUID, ...], query_profiles: tuple[str, ...]
) -> None:
    for need in needs:
        if not set(need.known_evidence).issubset(evidence_ids):
            raise ScopeDeniedError("Information need refers to evidence outside its input packet.")
        if not set(need.query_profiles).issubset(query_profiles):
            raise ScopeDeniedError("Information need requests an unavailable retrieval profile.")


def admit_action_frame(
    frame: ModelActionFrame,
    registry: ToolRegistry,
    authority: ProposalAuthority,
    facts: Mapping[str, object],
) -> ProposalAdmission:
    """Check model claims against controller data, without persisting or dispatching."""
    if frame.identity != authority.identity:
        raise StateMismatchError("Model frame echoes stale or unrelated controller identities.")
    _check_needs(frame.information_needs, authority.evidence_ids, authority.query_profiles)
    decisions = []
    for proposal in frame.actions:
        definition = registry.get(proposal.action.kind)
        definition.admit(proposal.action)
        if definition.name not in authority.allowed_tools:
            raise ScopeDeniedError("The model proposed a tool outside its rendered inventory.")
        for values, permitted, label in (
            (proposal.evidence_ids, authority.evidence_ids, "evidence"),
            (proposal.information_need_ids, authority.information_need_ids, "information need"),
            (proposal.prior_dependencies, authority.dependency_ids, "dependency"),
            (proposal.criterion_ids, authority.criterion_ids, "criterion"),
        ):
            if not set(values).issubset(permitted):
                raise ScopeDeniedError(f"Model proposal names an unavailable {label} reference.")
        if proposal.resource_class not in authority.resource_classes:
            raise ScopeDeniedError("The model proposed an unavailable resource class.")
        preconditions = tuple(evaluate_predicate(item, facts) for item in proposal.preconditions)
        stops = tuple(evaluate_predicate(item, facts) for item in proposal.stop_conditions)
        if PredicateResult.SATISFIED in stops:
            status = "stopped"
        elif PredicateResult.UNSATISFIED in preconditions:
            status = "blocked"
        elif PredicateResult.UNKNOWN in (*preconditions, *stops):
            status = "unknown"
        else:
            status = "admitted"
        decisions.append(ProposalDecision(proposal.proposal_id, status, preconditions, stops))
    # The controller schedules dependencies only after durable admission. Partial
    # frames would otherwise accidentally admit a dependent whose prerequisite failed.
    accepted = all(decision.status == "admitted" for decision in decisions)
    return ProposalAdmission(
        "admitted" if accepted else "blocked",
        frame.actions if accepted else (),
        tuple(decisions),
    )


def action_registry_subset(
    registry: ToolRegistry, allowed_tools: tuple[str, ...]
) -> tuple[dict[str, Any], ...]:
    """Expose the relevant installed inventory without executable paths or secrets."""
    _strings(allowed_tools, "allowed_tools")
    return tuple(
        {
            "kind": definition.name,
            "capability": definition.capability,
            "definition_digest": definition.fingerprint,
            "allows_arguments": definition.allow_arguments,
            "mutates_repository": definition.mutates,
            "timeout_ms": definition.timeout_ms,
            "accepts_stdin_artifact": definition.accepts_stdin,
        }
        for definition in (registry.get(name) for name in sorted(allowed_tools))
    )


@dataclass(frozen=True, slots=True)
class ModelCodingResponse:
    implementation_id: UUID
    base_state_id: UUID
    contract_revision_id: UUID
    patch: bytes | None = None
    inability_code: str | None = None
    inability_reason: str | None = None
    changed_files: tuple[str, ...] = ()
    proposed_checks: tuple[str, ...] = ()
    information_needs: tuple[NeedSpec, ...] = ()
    evidence_ids: tuple[UUID, ...] = ()
    assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.as_scripted_response()  # Reuse the existing mutually exclusive outcome checks.
        if not isinstance(self.contract_revision_id, UUID):
            raise InvalidInputError("Coding output must echo the exact task contract revision.")
        _strings(self.changed_files, "changed_files")
        _strings(self.proposed_checks, "proposed_checks")
        for path in self.changed_files:
            coding_path(path)
        _needs(self.information_needs)
        _uuids(self.evidence_ids, "evidence_ids")
        if self.patch is not None and not self.changed_files:
            raise InvalidInputError("A model patch must declare its changed files.")
        if self.patch is None and self.changed_files:
            raise InvalidInputError("An inability cannot claim changed files.")

    def as_scripted_response(self) -> ScriptedCodingResponse:
        """Return the old value contract only; this does not assert scripted provenance."""
        return ScriptedCodingResponse(
            self.implementation_id,
            self.base_state_id,
            self.patch,
            self.inability_code,
            self.inability_reason,
            self.assumptions,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "response_schema": CODING_RESPONSE_SCHEMA,
            "implementation_id": str(self.implementation_id),
            "base_state_id": str(self.base_state_id),
            "contract_revision_id": str(self.contract_revision_id),
            "patch": self.patch.decode("utf-8") if self.patch is not None else None,
            "inability_code": self.inability_code,
            "inability_reason": self.inability_reason,
            "changed_files": list(self.changed_files),
            "proposed_checks": list(self.proposed_checks),
            "information_needs": [need.to_dict() for need in self.information_needs],
            "evidence_ids": list(map(str, self.evidence_ids)),
            "assumptions": list(self.assumptions),
        }

    def to_result_dict(self, patch_artifact_id: UUID | None = None) -> dict[str, Any]:
        """Use the existing coding_results payload with explicit model provenance."""
        if patch_artifact_id is not None and not isinstance(patch_artifact_id, UUID):
            raise InvalidInputError("Recorded coding patch requires a typed artifact identity.")
        if (self.patch is None) != (patch_artifact_id is None):
            raise InvalidInputError("Recorded coding result must identify its patch artifact.")
        payload = self.as_scripted_response().to_dict(patch_artifact_id)
        payload.update(
            producer_kind="model",
            contract_revision_id=str(self.contract_revision_id),
            changed_files=list(self.changed_files),
            proposed_checks=list(self.proposed_checks),
            information_needs=[need.to_dict() for need in self.information_needs],
            evidence_ids=list(map(str, self.evidence_ids)),
        )
        return payload

    @classmethod
    def from_dict(cls, value: object) -> ModelCodingResponse:
        item = _object(
            value,
            {*cls.__dataclass_fields__, "schema_version", "response_schema"},
            "Model coding response",
        )
        _version(item["schema_version"])
        if item["response_schema"] != CODING_RESPONSE_SCHEMA:
            raise InvalidInputError("Unexpected coding response schema.")
        patch = item["patch"]
        if patch is not None and not isinstance(patch, str):
            raise InvalidInputError("Model patch must be a UTF-8 string or null.")
        try:
            patch_bytes = patch.encode("utf-8") if patch is not None else None
        except UnicodeEncodeError:
            raise InvalidInputError("Model patch contains invalid Unicode.") from None
        return cls(
            implementation_id=_uuid(item["implementation_id"], "implementation_id"),
            base_state_id=_uuid(item["base_state_id"], "base_state_id"),
            contract_revision_id=_uuid(item["contract_revision_id"], "contract_revision_id"),
            patch=patch_bytes,
            inability_code=item["inability_code"],
            inability_reason=item["inability_reason"],
            changed_files=_string_array(item["changed_files"], "changed_files"),
            proposed_checks=_string_array(item["proposed_checks"], "proposed_checks"),
            information_needs=tuple(
                need_from_dict(raw)
                for raw in _array(
                    item["information_needs"], "information_needs", limit=MAX_INFORMATION_NEEDS
                )
            ),
            evidence_ids=_uuid_array(item["evidence_ids"], "evidence_ids"),
            assumptions=_string_array(item["assumptions"], "assumptions"),
        )


def admit_coding_response(
    response: ModelCodingResponse,
    *,
    implementation_id: UUID,
    base_state_id: UUID,
    contract_revision_id: UUID,
    permitted_paths: tuple[str, ...],
    known_checks: tuple[str, ...],
    evidence_ids: tuple[UUID, ...] = (),
    query_profiles: tuple[str, ...] = (),
) -> ModelCodingResponse:
    """Validate declarations; applicability and behavioral verification remain separate."""
    if (
        response.implementation_id != implementation_id
        or response.base_state_id != base_state_id
        or response.contract_revision_id != contract_revision_id
    ):
        raise StateMismatchError("Coding response echoes a stale or unrelated handoff.")
    if any(not within_scope(path, permitted_paths) for path in response.changed_files):
        raise ScopeDeniedError("Model declares changes outside the permitted handoff scope.")
    if not set(response.proposed_checks).issubset(known_checks):
        raise ScopeDeniedError("Model proposes an unregistered check reference.")
    if not set(response.evidence_ids).issubset(evidence_ids):
        raise ScopeDeniedError("Coding response refers to evidence outside its input packet.")
    _check_needs(response.information_needs, evidence_ids, query_profiles)
    return response
