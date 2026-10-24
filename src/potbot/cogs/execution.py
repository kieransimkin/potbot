"""Installed execution-envelope Cogs. Serialized values never select Python code."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import UUID

from potbot.cogs.contracts import CogSignature
from potbot.domain.serialization import canonical_json
from potbot.domain.syn import SynObject, SynRef, SynSnapshot, parse_type_ref
from potbot.semantics.embers import EmberContractBundle
from potbot.tools.execution_contracts import (
    EVENT,
    RECEIPT,
    REPLAY_REQUEST,
    REPLAY_RESULT,
    REQUEST,
    RESULT,
    SEAL_REQUEST,
    SEAL_RESULT,
    WORK_KINDS,
)

WORK_COG = "syn://type/potbot-work-cog"

if TYPE_CHECKING:
    from potbot.cogs.contracts import TypedCogValue
    from potbot.controller.execution_cogs import ExecutionCall


async def execute(
    call: ExecutionCall,
    handler: Callable[[ExecutionCall, TypedCogValue], Awaitable[TypedCogValue]],
    source: TypedCogValue,
) -> TypedCogValue:
    """Installed binding; a retained or fabricated callback has no active grant."""
    call._check()
    return await handler(call, source)


def signature(kind: str, source: str = REQUEST, target: str = RESULT) -> CogSignature:
    slug = "potbot-work-" + kind.replace("_", "-")
    return CogSignature(
        SynRef(f"syn://cog/{slug}-v1"),
        f"syn://type/{slug}-cog",
        source,
        target,
        "potbot.cogs.execution.execute",
        1,
        True,
    )


WORK_COGS = tuple(signature(kind) for kind in WORK_KINDS)
SEAL = signature("seal", SEAL_REQUEST, SEAL_RESULT)
REPLAY = signature("replay", REPLAY_REQUEST, REPLAY_RESULT)
EXECUTION_COGS = (*WORK_COGS, SEAL, REPLAY)


def execution_cog_snapshot() -> SynSnapshot:
    from potbot.cogs.debugger import debugger_cog_snapshot
    from potbot.cogs.registry import _type, builtin_cog_snapshot
    from potbot.cogs.tool_contracts import contract_cog_snapshot
    from potbot.tools.execution_schemas import schemas

    base = contract_cog_snapshot()
    properties = next(
        t.properties
        for t in debugger_cog_snapshot().types
        if str(t.ref) == "syn://type/potbot-debug-tool-cog"
    )
    types = [
        *base.types,
        _type(
            WORK_COG,
            "Pinned execution Cog",
            "syn://type/cog",
            "Host-admitted work with immutable provenance",
            properties,
        ),
    ]
    wire_schemas = schemas()
    for ref in (REQUEST, RESULT, SEAL_REQUEST, SEAL_RESULT, REPLAY_REQUEST, REPLAY_RESULT, RECEIPT):
        types.append(
            replace(
                _type(
                    ref,
                    ref.rsplit("/", 1)[-1],
                    "synny://type/potbot-data-record",
                    "Version 1 execution envelope; observation grants no authority",
                ),
                metadata_json=canonical_json(
                    {"module": "potbot.cogs", "ontologyVersion": 1, "schema": wire_schemas[ref]}
                ),
            )
        )
    types.append(
        _type(
            EVENT,
            "Pinned work event",
            "syn://type/event",
            "Existing workspace execution ledger observation, version 1",
        )
    )
    objects = list(builtin_cog_snapshot().objects)
    for item in EXECUTION_COGS:
        types.append(
            _type(
                item.cog_type_ref,
                item.cog_ref.slug,
                WORK_COG,
                "One typed input and output with mandatory work observations",
            )
        )
        objects.append(
            SynObject(
                item.cog_ref,
                parse_type_ref(item.cog_type_ref),
                canonical_json(
                    {
                        "id": str(item.cog_ref),
                        "typeRef": item.cog_type_ref,
                        "name": item.cog_ref.slug,
                        "schemaVersion": 1,
                        "inputTypeRef": item.input_type_ref,
                        "outputTypeRef": item.output_type_ref,
                        "implementationBindingHash": item.binding_hash,
                        "handlerRef": item.handler_ref,
                        "implementationRevision": 1,
                        "requiresControllerScope": True,
                    }
                ),
            )
        )
    return replace(
        base,
        source_label="potbot-execution-provenance-v1",
        types=tuple(types),
        objects=tuple(objects),
    )


def execution_ember_bundle(
    *, workspace_id: UUID, snapshot_id: UUID, namespace: str
) -> EmberContractBundle:
    from potbot.cogs.tool_contracts import contract_ember_bundle

    return contract_ember_bundle(
        workspace_id=workspace_id,
        snapshot_id=snapshot_id,
        namespace=namespace,
        _snapshot=execution_cog_snapshot(),
        _installed=EXECUTION_COGS,
        _cog_type=WORK_COG,
    )
