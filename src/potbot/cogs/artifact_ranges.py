"""Installed artifact open/read/close Cogs on the shared pinned execution boundary."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.cogs.contracts import CogSignature, TypedCogValue
from potbot.cogs.execution import EXECUTION_COGS, WORK_COG, execution_cog_snapshot
from potbot.domain.serialization import canonical_json
from potbot.domain.syn import SynObject, SynRef, SynSnapshot, parse_type_ref
from potbot.tools.artifact_ranges import (
    CLOSE_REQUEST,
    CLOSE_RESULT,
    OPEN_REQUEST,
    OPEN_RESULT,
    READ_REQUEST,
    READ_RESULT,
    schemas,
)

if TYPE_CHECKING:
    from potbot.controller.execution_cogs import HostOperation
    from potbot.semantics.embers import EmberContractBundle


def _signature(name: str, source: str, target: str) -> CogSignature:
    return CogSignature(
        SynRef(f"syn://cog/potbot-artifact-{name}-v1"),
        f"syn://type/potbot-artifact-{name}-cog",
        source,
        target,
        "potbot.cogs.execution.execute",
        1,
        True,
    )


OPEN = _signature("open-read", OPEN_REQUEST, OPEN_RESULT)
READ = _signature("read-range", READ_REQUEST, READ_RESULT)
CLOSE = _signature("close-read", CLOSE_REQUEST, CLOSE_RESULT)
RANGE_COGS = (OPEN, READ, CLOSE)


def artifact_range_snapshot() -> SynSnapshot:
    from potbot.cogs.registry import _type

    base = execution_cog_snapshot()
    types, objects = list(base.types), list(base.objects)
    for ref, schema in schemas().items():
        types.append(
            replace(
                _type(
                    ref,
                    ref.rsplit("/", 1)[-1],
                    "synny://type/potbot-data-record",
                    "Bounded artifact read contract; admission is independently supplied",
                ),
                metadata_json=canonical_json(
                    {"module": "potbot.cogs", "ontologyVersion": 1, "schema": schema}
                ),
            )
        )
    for item in RANGE_COGS:
        types.append(
            _type(
                item.cog_type_ref,
                item.cog_ref.slug,
                WORK_COG,
                "Installed bounded artifact observation",
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
        base, source_label="potbot-artifact-ranges-v1", types=tuple(types), objects=tuple(objects)
    )


def artifact_range_ember_bundle(
    *, workspace_id: UUID, snapshot_id: UUID, namespace: str
) -> EmberContractBundle:
    from potbot.cogs.tool_contracts import contract_ember_bundle

    return contract_ember_bundle(
        workspace_id=workspace_id,
        snapshot_id=snapshot_id,
        namespace=namespace,
        _snapshot=artifact_range_snapshot(),
        _installed=(*EXECUTION_COGS, *RANGE_COGS),
        _cog_type=WORK_COG,
    )


def installed_operations(service: Any, policy: Any) -> dict[CogSignature, HostOperation]:
    from potbot.controller.execution_cogs import HostOperation
    from potbot.tools.execution_contracts import WorkBinding

    operations = {}
    for signature, method in zip(
        RANGE_COGS, (service.open, service.read, service.close), strict=True
    ):

        async def handler(
            call: Any,
            source: TypedCogValue,
            method: Any = method,
            signature: CogSignature = signature,
        ) -> TypedCogValue:
            call.progress("artifact_request_validated")
            result = method(source.value)
            call.runtime._admission(signature, source, call.work.context)
            call.progress(
                "artifact_range_verified" if signature == READ else "artifact_lease_persisted"
            )
            return TypedCogValue(signature.output_type_ref, result)

        operations[signature] = HostOperation(
            WorkBinding(
                "tool",
                f"syn://thing/{signature.cog_ref.slug}",
                signature.input_type_ref,
                signature.output_type_ref,
                signature.binding_hash,
                "read_only" if signature == READ else "idempotent_write",
                policy,
            ),
            handler,
        )
    return operations
