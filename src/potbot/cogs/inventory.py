"""Installed single-input/single-output source inventory Cog."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from potbot.cogs.contracts import CogSignature, TypedCogValue
from potbot.domain.serialization import canonical_json
from potbot.domain.syn import SynObject, SynRef, SynSnapshot, parse_type_ref

if TYPE_CHECKING:
    from potbot.controller.tool_inventory import ToolInventoryRuntime

SIGNATURE = CogSignature(
    SynRef("syn://cog/potbot-tool-inventory-v1"),
    "syn://type/potbot-tool-inventory-cog",
    "syn://type/potbot-tool-inventory-request",
    "syn://type/potbot-tool-inventory-result",
    "potbot.cogs.inventory.execute",
    1,
    True,
)


def inventory_cog_snapshot() -> SynSnapshot:
    from potbot.cogs.debugger import debugger_cog_snapshot
    from potbot.cogs.registry import _type, builtin_cog_snapshot

    # Reuse the existing native Cog's inherited declarations, without duplicating its root.
    parent = next(
        t for t in debugger_cog_snapshot().types if str(t.ref) == "syn://type/potbot-debug-tool-cog"
    )
    base = builtin_cog_snapshot()
    definitions = (
        _type(
            SIGNATURE.cog_type_ref,
            "Source inventory Cog",
            "syn://type/cog",
            "Controller-admitted source inventory",
            parent.properties,
        ),
        _type(
            SIGNATURE.input_type_ref,
            "Inventory request",
            "synny://type/potbot-data-record",
            "Expected immutable content digest and enabled inventory profile",
        ),
        _type(
            SIGNATURE.output_type_ref,
            "Inventory result",
            "synny://type/potbot-data-record",
            "Source-only inventory and qualification limits",
        ),
    )
    obj = SynObject(
        ref=SIGNATURE.cog_ref,
        type_ref=parse_type_ref(SIGNATURE.cog_type_ref),
        payload_json=canonical_json(
            {
                "id": str(SIGNATURE.cog_ref),
                "typeRef": SIGNATURE.cog_type_ref,
                "name": "tool source inventory",
                "schemaVersion": 1,
                "inputTypeRef": SIGNATURE.input_type_ref,
                "outputTypeRef": SIGNATURE.output_type_ref,
                "implementationBindingHash": SIGNATURE.binding_hash,
                "handlerRef": SIGNATURE.handler_ref,
                "implementationRevision": SIGNATURE.implementation_revision,
                "requiresControllerScope": True,
            }
        ),
    )
    return SynSnapshot(
        source_label="potbot-tool-inventory-cogs-v1",
        catalog_mode="extend_builtin",
        types=(*base.types, *definitions),
        objects=(*base.objects, obj),
    )


async def execute(runtime: ToolInventoryRuntime, source: TypedCogValue) -> dict[str, Any]:
    return await runtime.dispatch(source)
