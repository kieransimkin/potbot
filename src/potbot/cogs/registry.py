"""Sealed built-in Cog definitions, exact input/output typing and traceable execution.

The SYN snapshot contains both concrete data types and typed Cog instances. The
runtime verifies that its installed handler binding agrees with that snapshot;
it never loads Python implementations from untrusted Cog metadata.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from potbot.debugging.cog_runtime import DebuggerCogRuntime

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import canonical_json
from potbot.domain.syn import (
    SynObject,
    SynProperty,
    SynRef,
    SynSnapshot,
    SynType,
    parse_type_ref,
    type_identity,
)
from potbot.semantics.catalog import merge_types, type_ancestors

from . import builtin
from .contracts import (
    CodingScope,
    CogOutcome,
    CogReceipt,
    CogRunContext,
    CogSignature,
    TypedCogValue,
    check_cog_input,
    check_cog_output,
)

TRANSFORM_TYPE = "syn://type/potbot-transform-cog"
PARSE_JSON_TYPE = "syn://type/potbot-parse-completion-json-cog"
PARSE_CODING_TYPE = "syn://type/potbot-parse-model-coding-cog"
PARSE_ACTION_TYPE = "syn://type/potbot-parse-model-action-cog"
SCOPE_CODING_TYPE = "syn://type/potbot-scope-check-model-coding-cog"

COG_COMPLETION_JSON = "syn://cog/potbot-completion-json-v1"
COG_JSON_CODING = "syn://cog/potbot-json-model-coding-v1"
COG_JSON_ACTION = "syn://cog/potbot-json-model-action-v1"
COG_SCOPE_CODING = "syn://cog/potbot-scope-check-coding-v1"


def _signature(
    cog_ref: str,
    cog_type: str,
    input_type: str,
    output_type: str,
    handler_name: str,
    *,
    scoped: bool = False,
) -> CogSignature:
    return CogSignature(
        SynRef(cog_ref),
        cog_type,
        input_type,
        output_type,
        "potbot.cogs.builtin." + handler_name,
        1,
        scoped,
    )


INSTALLED_COGS: tuple[CogSignature, ...] = (
    _signature(
        COG_COMPLETION_JSON,
        PARSE_JSON_TYPE,
        builtin.COMPLETION_TEXT,
        builtin.JSON_DOCUMENT,
        "text_to_json",
    ),
    _signature(
        COG_JSON_CODING,
        PARSE_CODING_TYPE,
        builtin.JSON_DOCUMENT,
        builtin.MODEL_CODING_RESPONSE,
        "json_to_coding",
    ),
    _signature(
        COG_JSON_ACTION,
        PARSE_ACTION_TYPE,
        builtin.JSON_DOCUMENT,
        builtin.MODEL_ACTION_FRAME,
        "json_to_action",
    ),
    _signature(
        COG_SCOPE_CODING,
        SCOPE_CODING_TYPE,
        builtin.MODEL_CODING_RESPONSE,
        builtin.SCOPE_CHECKED_CODING,
        "coding_scope_check",
        scoped=True,
    ),
)


def _type(
    ref: str, name: str, parent: str, definition: str, properties: tuple[SynProperty, ...] = ()
) -> SynType:
    return SynType(
        ref=parse_type_ref(ref),
        name=name,
        parents=(parse_type_ref(parent),),
        definition=definition,
        properties=properties,
        metadata_json=canonical_json({"module": "potbot.cogs", "ontologyVersion": 1}),
    )


def builtin_cog_snapshot() -> SynSnapshot:
    """Public, importable SYN definitions; safe as data in any workspace.

    Importing this snapshot does NOT grant any Cog a tool capability, nor does it
    install source code from the snapshot. The importer seals a repository-scoped
    catalog; callers should pin its effective released snapshot separately.
    """
    fields = (
        SynProperty("inputTypeRef", SynRef("syn://type/type")),
        SynProperty("outputTypeRef", SynRef("syn://type/type")),
        SynProperty(
            "implementationBindingHash", parse_type_ref("synny://type/potbot-data-value-string")
        ),
    )
    definitions = (
        _type(
            TRANSFORM_TYPE,
            "Potbot transform Cog",
            "syn://type/cog",
            "Cog that transforms exactly one declared input data Type into one declared output data Type.",
            fields,
        ),
        _type(
            PARSE_JSON_TYPE,
            "Parse model completion as JSON",
            TRANSFORM_TYPE,
            "Strict, bounded JSON syntax conversion; never grants execution authority.",
        ),
        _type(
            PARSE_CODING_TYPE,
            "Decode model coding response",
            TRANSFORM_TYPE,
            "Checks a JSON document against the coding result contract; output is untrusted.",
        ),
        _type(
            PARSE_ACTION_TYPE,
            "Decode model action proposal",
            TRANSFORM_TYPE,
            "Checks a JSON document against the action frame contract; output is untrusted.",
        ),
        _type(
            SCOPE_CODING_TYPE,
            "Scope-check a model coding response",
            TRANSFORM_TYPE,
            "Uses separately supplied controller authority; does not settle usage or persist a patch.",
        ),
        _type(
            builtin.COMPLETION_TEXT,
            "Completed provider text",
            "synny://type/potbot-data-text",
            "A completed, retained provider text result; completion status must be checked by the caller.",
        ),
        _type(
            builtin.MODEL_CODING_RESPONSE,
            "Structured model coding response",
            "synny://type/potbot-data-record",
            "A schema-checked model coding response, not an admitted executable patch.",
        ),
        _type(
            builtin.MODEL_ACTION_FRAME,
            "Structured model action proposal",
            "synny://type/potbot-data-record",
            "A schema-checked action proposal frame, not a dispatched tool action.",
        ),
        _type(
            builtin.SCOPE_CHECKED_CODING,
            "Scope-checked model coding response",
            builtin.MODEL_CODING_RESPONSE,
            "Matches controller-supplied coding handoff scope; additional durable and budget checks required.",
        ),
    )
    objects = tuple(
        SynObject(
            ref=signature.cog_ref,
            type_ref=parse_type_ref(signature.cog_type_ref),
            payload_json=canonical_json(
                {
                    "id": str(signature.cog_ref),
                    "name": signature.cog_ref.slug,
                    "typeRef": signature.cog_type_ref,
                    "schemaVersion": 1,
                    "inputTypeRef": signature.input_type_ref,
                    "outputTypeRef": signature.output_type_ref,
                    "implementationBindingHash": signature.binding_hash,
                    "handlerRef": signature.handler_ref,
                    "implementationRevision": signature.implementation_revision,
                    "requiresControllerScope": signature.requires_controller_scope,
                }
            ),
        )
        for signature in INSTALLED_COGS
    )
    return SynSnapshot(
        source_label="potbot-cog-data-transformations-v1",
        catalog_mode="extend_builtin",
        types=definitions,
        objects=objects,
    )


@dataclass(frozen=True, slots=True)
class BoundCogRegistry:
    """Only locally installed, snapshot-checked implementations are runnable."""

    snapshot: SynSnapshot
    execution_profile: bool = False

    def installed(self) -> tuple[CogSignature, ...]:
        if self.execution_profile:
            from potbot.cogs.debugger import DEBUGGER_COGS

            return (*INSTALLED_COGS, *DEBUGGER_COGS)
        return INSTALLED_COGS

    def __post_init__(self) -> None:
        from potbot.cogs.debugger import debugger_cog_snapshot

        expected = debugger_cog_snapshot() if self.execution_profile else builtin_cog_snapshot()
        if self.snapshot.fingerprint != expected.fingerprint:
            raise ScopeDeniedError("Cog catalogue must match the installed pinned definitions")
        declared = {type_identity(t.ref): t for t in merge_types(self.snapshot)}
        closure = type_ancestors(declared.values())
        installed = self.installed()
        signatures = {str(signature.cog_ref): signature for signature in installed}
        for entry in self.snapshot.objects:
            signature = signatures.get(str(entry.ref))
            if signature is None:
                raise ScopeDeniedError("Uninstalled Cog cannot be executed")
            if type_identity(entry.type_ref) != type_identity(signature.cog_type_ref):
                raise ScopeDeniedError("Cog object and installed concrete type disagree")
            ancestors = closure[str(parse_type_ref(signature.cog_type_ref))]
            if "syn://type/cog" not in ancestors:
                raise ScopeDeniedError("Concrete transform Cog must descend from native Cog")
            if any(
                type_identity(ref) not in declared
                for ref in (
                    signature.input_type_ref,
                    signature.output_type_ref,
                )
            ):
                raise ScopeDeniedError("Cog references an unregistered input or output Type")
            payload = json.loads(entry.payload_json)
            if (
                payload["inputTypeRef"] != signature.input_type_ref
                or payload["outputTypeRef"] != signature.output_type_ref
                or payload["implementationBindingHash"] != signature.binding_hash
                or payload["handlerRef"] != signature.handler_ref
                or payload["implementationRevision"] != signature.implementation_revision
                or payload["requiresControllerScope"] != signature.requires_controller_scope
            ):
                raise ScopeDeniedError("Cog public Ember declarations differ from installed code")
        if len(self.snapshot.objects) != len(installed):
            raise ScopeDeniedError("Incomplete Cog catalogue")
        if any(s.handler_ref not in builtin.BUILTIN_HANDLERS for s in INSTALLED_COGS):
            raise ScopeDeniedError("Cog handler binding is not installed")

    @classmethod
    def builtins(cls) -> BoundCogRegistry:
        return cls(builtin_cog_snapshot())

    @classmethod
    def debugger(cls) -> BoundCogRegistry:
        from potbot.cogs.debugger import debugger_cog_snapshot

        return cls(debugger_cog_snapshot(), execution_profile=True)

    async def invoke(
        self,
        cog_ref: str,
        source: TypedCogValue,
        context: CogRunContext,
        *,
        controller: DebuggerCogRuntime,
    ) -> CogOutcome:
        """Explicit async execution path. Pure transform() never dispatches tools."""
        from potbot.cogs.debugger import DEBUGGER_COGS
        from potbot.debugging.cog_runtime import DebuggerCogRuntime

        if not self.execution_profile or not isinstance(controller, DebuggerCogRuntime):
            raise ScopeDeniedError("Execution Cogs require their installed controller profile")
        selected = next((s for s in DEBUGGER_COGS if str(s.cog_ref) == cog_ref), None)
        if selected is None or selected.handler_ref != "potbot.cogs.debugger.execute":
            raise ScopeDeniedError("Execution Cog handler is not installed")
        if context.cog_snapshot_hash != self.snapshot.fingerprint:
            raise StateMismatchError("Execution Cog catalog changed")
        check_cog_input(selected, source)
        # Freeze the serialized value before an async boundary; the caller's
        # mutable dict cannot alter a dispatched request or its later receipt.
        source = TypedCogValue(
            source.type_ref, json.loads(canonical_json(source.value)), source.source_artifact_id
        )
        return await controller.invoke(selected, source, context)

    def transform(
        self,
        cog_ref: str,
        source: TypedCogValue,
        context: CogRunContext,
        *,
        scope: CodingScope | None = None,
    ) -> CogOutcome:
        if (
            not isinstance(context, CogRunContext)
            or context.cog_snapshot_hash != self.snapshot.fingerprint
        ):
            raise StateMismatchError("Cog transform requires the exact pinned catalogue revision")
        if not isinstance(source, TypedCogValue):
            raise InvalidInputError("Cog input must carry one checked Type")
        selected = next((s for s in INSTALLED_COGS if str(s.cog_ref) == cog_ref), None)
        if selected is None:
            raise ScopeDeniedError("Cog is not installed or is not part of this catalogue")
        check_cog_input(selected, source)
        if selected.requires_controller_scope != (scope is not None):
            raise ScopeDeniedError("Cog controller-scope requirement is not satisfied")
        if scope is not None and scope.contract_revision_id != context.task_revision_id:
            raise StateMismatchError("Cog controller authority is from another task revision")
        output = builtin.BUILTIN_HANDLERS[selected.handler_ref](source, scope)
        check_cog_output(selected, output)
        receipt = CogReceipt(
            cog_ref=str(selected.cog_ref),
            cog_type_ref=selected.cog_type_ref,
            binding_hash=selected.binding_hash,
            cog_snapshot_hash=context.cog_snapshot_hash,
            task_revision_id=context.task_revision_id,
            repository_state_id=context.repository_state_id,
            invocation_id=context.invocation_id,
            input_type_ref=source.type_ref,
            input_hash=source.fingerprint,
            output_type_ref=output.type_ref,
            output_hash=output.fingerprint,
            input_artifact_id=source.source_artifact_id,
            upstream_receipt_hash=context.upstream_receipt_hash,
            scope_hash=scope.fingerprint if scope is not None else None,
        )
        return CogOutcome(output, receipt)

    def plan(
        self, input_type_ref: str, output_type_ref: str, *, controller_scope: bool = False
    ) -> tuple[str, ...]:
        """Find an explicit installed Cog chain by exact registered Type identity.

        Never guess a conversion from embeddings, model prose, class ancestry or
        compatible-looking JSON. If two equally short routes exist, a caller
        must pick an explicit chain rather than rely on arbitrary ordering.
        """
        source, target = type_identity(input_type_ref), type_identity(output_type_ref)
        known = {type_identity(t.ref) for t in merge_types(self.snapshot)}
        if source not in known or target not in known:
            raise ScopeDeniedError("Conversion requires registered source and target Types")
        if source == target:
            raise InvalidInputError("An identity conversion is not a Cog transformation")
        edges: dict[str, list[CogSignature]] = {}
        for signature in INSTALLED_COGS:
            if signature.requires_controller_scope and not controller_scope:
                continue
            edges.setdefault(type_identity(signature.input_type_ref), []).append(signature)
        frontier: list[tuple[str, tuple[str, ...]]] = [(source, ())]
        best_depth = {source: 0}
        solutions: list[tuple[str, ...]] = []
        for _ in range(16):
            next_frontier: list[tuple[str, tuple[str, ...]]] = []
            for current, path in frontier:
                for signature in sorted(edges.get(current, ()), key=lambda item: str(item.cog_ref)):
                    next_type = type_identity(signature.output_type_ref)
                    new_path = (*path, str(signature.cog_ref))
                    if next_type == target:
                        solutions.append(new_path)
                    if next_type not in best_depth or len(new_path) <= best_depth[next_type]:
                        best_depth[next_type] = len(new_path)
                        next_frontier.append((next_type, new_path))
            if solutions:
                if len(set(solutions)) != 1:
                    raise StateMismatchError(
                        "Multiple equally short Cog paths require explicit selection"
                    )
                return solutions[0]
            if not next_frontier:
                break
            frontier = next_frontier
        raise StateMismatchError("No registered Cog conversion path connects these Types")

    def convert(
        self,
        source: TypedCogValue,
        output_type_ref: str,
        context: CogRunContext,
        *,
        scope: CodingScope | None = None,
    ) -> tuple[CogOutcome, ...]:
        """Discover the typed path and execute only its installed Cog instances."""
        route = self.plan(source.type_ref, output_type_ref, controller_scope=scope is not None)
        return self.chain(route, source, context, scope=scope)

    def chain(
        self,
        cogs: tuple[str, ...],
        source: TypedCogValue,
        context: CogRunContext,
        *,
        scope: CodingScope | None = None,
    ) -> tuple[CogOutcome, ...]:
        """Explicit typed composition, never an implicit conversion path search."""
        if not isinstance(cogs, tuple) or not 1 <= len(cogs) <= 16:
            raise InvalidInputError("Cog composition requires a bounded nonempty tuple")
        if scope is not None and COG_SCOPE_CODING not in cogs:
            raise ScopeDeniedError("Controller scope cannot be supplied to an unrelated Cog chain")
        outcomes: list[CogOutcome] = []
        current = source
        upstream = context.upstream_receipt_hash
        for cog_ref in cogs:
            next_context = CogRunContext(
                cog_snapshot_hash=context.cog_snapshot_hash,
                task_revision_id=context.task_revision_id,
                repository_state_id=context.repository_state_id,
                invocation_id=context.invocation_id,
                upstream_receipt_hash=upstream,
            )
            selected_scope = scope if cog_ref == COG_SCOPE_CODING else None
            outcome = self.transform(cog_ref, current, next_context, scope=selected_scope)
            current = outcome.value
            upstream = outcome.receipt.fingerprint
            outcomes.append(outcome)
        return tuple(outcomes)
