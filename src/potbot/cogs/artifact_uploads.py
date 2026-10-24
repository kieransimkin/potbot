"""Installed upload tools on the shared pinned SYN execution boundary."""

from __future__ import annotations

from dataclasses import replace
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.cogs.artifact_ranges import RANGE_COGS, _signature, artifact_range_snapshot
from potbot.cogs.contracts import CogSignature, TypedCogValue
from potbot.cogs.execution import EXECUTION_COGS, WORK_COG
from potbot.domain.errors import OperationTimeoutError
from potbot.domain.serialization import canonical_json
from potbot.domain.syn import SynObject, SynSnapshot, parse_type_ref
from potbot.tools.artifact_uploads import NAMES, PORTS, schemas

if TYPE_CHECKING:
    from potbot.controller.execution_cogs import HostOperation
    from potbot.semantics.embers import EmberContractBundle

UPLOAD_COGS = tuple(_signature(name, *ports) for name, ports in zip(NAMES, PORTS, strict=True))
OPEN, CHUNK, INSPECT, COMMIT, ABORT = UPLOAD_COGS


def artifact_upload_snapshot() -> SynSnapshot:
    from potbot.cogs.registry import _type

    base = artifact_range_snapshot()
    types, objects = list(base.types), list(base.objects)
    for ref, schema in schemas().items():
        types.append(
            replace(
                _type(
                    ref,
                    ref.rsplit("/", 1)[-1],
                    "synny://type/potbot-data-record",
                    "Bounded artifact upload contract; independent host admission is required",
                ),
                metadata_json=canonical_json(
                    {"module": "potbot.cogs", "ontologyVersion": 1, "schema": schema}
                ),
            )
        )
    for item in UPLOAD_COGS:
        types.append(
            _type(
                item.cog_type_ref, item.cog_ref.slug, WORK_COG, "Installed bounded artifact upload"
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
        base, source_label="potbot-artifact-uploads-v1", types=tuple(types), objects=tuple(objects)
    )


def artifact_upload_ember_bundle(
    *, workspace_id: UUID, snapshot_id: UUID, namespace: str
) -> EmberContractBundle:
    from potbot.cogs.tool_contracts import contract_ember_bundle

    return contract_ember_bundle(
        workspace_id=workspace_id,
        snapshot_id=snapshot_id,
        namespace=namespace,
        _snapshot=artifact_upload_snapshot(),
        _installed=(*EXECUTION_COGS, *RANGE_COGS, *UPLOAD_COGS),
        _cog_type=WORK_COG,
    )


def installed_operations(service: Any, policy: Any) -> dict[CogSignature, HostOperation]:
    from potbot.controller.execution_cogs import HostOperation
    from potbot.tools.execution_contracts import WorkBinding

    operations = {}
    for signature, method in zip(
        UPLOAD_COGS,
        (service.open, service.chunk, service.inspect, service.commit, service.abort),
        strict=True,
    ):

        async def handler(
            call: Any,
            source: TypedCogValue,
            method: Any = method,
            signature: CogSignature = signature,
        ) -> TypedCogValue:
            started = monotonic()

            def authorize() -> None:
                call._check()
                if (monotonic() - started) * 1000 >= policy.max_wall_ms:
                    raise OperationTimeoutError("Upload exceeded its admitted wall-time budget.")
                call.runtime._admission(signature, source, call.work.context)

            call.progress("upload_request_validated")
            result = method(source.value, authorize=authorize)
            authorize()
            call.progress(
                "upload_state_observed" if signature == INSPECT else "upload_state_persisted"
            )
            return TypedCogValue(signature.output_type_ref, result)

        operations[signature] = HostOperation(
            WorkBinding(
                "tool",
                f"syn://thing/{signature.cog_ref.slug}",
                signature.input_type_ref,
                signature.output_type_ref,
                signature.binding_hash,
                "read_only" if signature == INSPECT else "idempotent_write",
                policy,
            ),
            handler,
        )
    return operations
