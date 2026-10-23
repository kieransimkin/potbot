"""Installed canonical contract Cogs and their inherited Ember declarations."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.cogs.contracts import CogSignature, TypedCogValue
from potbot.domain.serialization import canonical_json
from potbot.domain.syn import SynObject, SynRef, SynSnapshot, parse_type_ref
from potbot.semantics.catalog import merge_types
from potbot.semantics.embers import (
    EmberBinding,
    EmberContractBundle,
    EmberInstance,
    EmberRevision,
    EmberSlot,
    TypeEmberContract,
    build_bundle,
)
from potbot.tools.semantic_contracts import CHECK, CHECKED, QUERY, RESULTS, TABLE, schemas

if TYPE_CHECKING:
    from potbot.controller.tool_contracts import ToolContractRuntime

TOOL_COG = "syn://type/potbot-canonical-tool-cog"


def signature(operation: str, source: str, target: str) -> CogSignature:
    return CogSignature(
        SynRef(f"syn://cog/potbot-{operation}-v1"),
        f"syn://type/potbot-{operation}-cog",
        source,
        target,
        "potbot.cogs.tool_contracts.execute",
        1,
        True,
    )


SEARCH = signature("contract-search", QUERY, RESULTS)
CONVERT = signature("contract-convert-units", TABLE, TABLE)
VALIDATE = signature("contract-validate", CHECK, CHECKED)
CONTRACT_COGS = (SEARCH, CONVERT, VALIDATE)


def contract_cog_snapshot() -> SynSnapshot:
    from potbot.cogs.debugger import debugger_cog_snapshot
    from potbot.cogs.registry import _type, builtin_cog_snapshot
    from potbot.semantics.hierarchy import profile_snapshot

    base = builtin_cog_snapshot()
    fields = next(
        t.properties
        for t in debugger_cog_snapshot().types
        if str(t.ref) == "syn://type/potbot-debug-tool-cog"
    )
    types = [
        *base.types,
        *profile_snapshot().types,
        _type(
            TOOL_COG,
            "Canonical tool Cog",
            "syn://type/cog",
            "Installed controller-admitted contract operation",
            fields,
        ),
    ]
    for ref, schema in schemas().items():
        types.append(
            replace(
                _type(
                    ref,
                    ref.rsplit("/", 1)[-1],
                    "synny://type/potbot-data-record",
                    "Version 1 canonical semantic contract; data grants no authority",
                ),
                metadata_json=canonical_json(
                    {"module": "potbot.cogs", "ontologyVersion": 1, "schema": schema}
                ),
            )
        )
    objects = list(base.objects)
    for installed in CONTRACT_COGS:
        types.append(
            _type(
                installed.cog_type_ref,
                installed.cog_ref.slug,
                TOOL_COG,
                "One typed input/output, pinned installed handler and mandatory monitoring",
            )
        )
        objects.append(
            SynObject(
                installed.cog_ref,
                parse_type_ref(installed.cog_type_ref),
                canonical_json(
                    {
                        "id": str(installed.cog_ref),
                        "typeRef": installed.cog_type_ref,
                        "name": installed.cog_ref.slug,
                        "schemaVersion": 1,
                        "inputTypeRef": installed.input_type_ref,
                        "outputTypeRef": installed.output_type_ref,
                        "implementationBindingHash": installed.binding_hash,
                        "handlerRef": installed.handler_ref,
                        "implementationRevision": 1,
                        "requiresControllerScope": True,
                    }
                ),
            )
        )
    return SynSnapshot(
        "potbot-canonical-tool-contracts-v1", "extend_builtin", tuple(types), tuple(objects)
    )


def contract_ember_bundle(
    *, workspace_id: UUID, snapshot_id: UUID, namespace: str
) -> EmberContractBundle:
    """Use the existing resolver for inherited required/default/optional slots."""
    text = "synny://type/potbot-data-value-string"
    declarations, embers, defaults, instances = [], [], [], []
    for name, minimum in (
        ("input-type", 1),
        ("output-type", 1),
        ("binding-hash", 1),
        ("schema-version", 1),
        ("description", 0),
    ):
        declarations.append(
            EmberSlot(
                f"syn://thing/potbot-contract-{name}",
                name,
                "syn://type/ember",
                text,
                minimum,
                1,
                source="effective",
                override="fixed",
            )
        )
    default = EmberRevision.create(
        "syn://ember/potbot-contract-schema-version-v1", "syn://type/ember", text, "1"
    )
    embers.append(default)
    defaults.append(EmberBinding("syn://thing/potbot-contract-schema-version", (default.pointer,)))
    for installed in CONTRACT_COGS:
        bindings = []
        for name, value in (
            ("input-type", installed.input_type_ref),
            ("output-type", installed.output_type_ref),
            ("binding-hash", installed.binding_hash),
        ):
            ember = EmberRevision.create(
                f"syn://ember/{installed.cog_ref.slug}-{name}", "syn://type/ember", text, value
            )
            embers.append(ember)
            bindings.append(EmberBinding(f"syn://thing/potbot-contract-{name}", (ember.pointer,)))
        instances.append(
            EmberInstance.create(str(installed.cog_ref), installed.cog_type_ref, bindings)
        )
    return build_bundle(
        merge_types(contract_cog_snapshot()),
        workspace_id=workspace_id,
        namespace=namespace,
        snapshot_id=snapshot_id,
        type_contracts=(TypeEmberContract.create(TOOL_COG, declarations, defaults),),
        embers=embers,
        instances=instances,
    )


async def execute(
    runtime: ToolContractRuntime, operation: CogSignature, source: TypedCogValue
) -> dict[str, Any]:
    return await runtime.dispatch(operation, source)
