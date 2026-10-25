"""Four explicit SYN Browser Cogs installed on the *existing* work ledger.

No arbitrary browser tool executes as a host Python callback. These direct
operators are the same trusted control-plane exception as artifact leases:
they forward only registered commands into an already sandboxed Chromium,
with controller admission, workspace ownership, and private evidence receipts.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.cogs.contracts import CogSignature, TypedCogValue
from potbot.cogs.execution import EXECUTION_COGS, WORK_COG, execution_cog_snapshot
from potbot.domain.errors import IndeterminateExecutionError
from potbot.domain.serialization import canonical_json
from potbot.domain.syn import SynObject, SynRef, SynSnapshot, parse_type_ref
from potbot.tools.browser_cog_contracts import (
    ACT_REQUEST,
    CAPTURE_REQUEST,
    EVIDENCE_RESULT,
    OBSERVE_REQUEST,
    PICK_REQUEST,
    schemas,
)

if TYPE_CHECKING:
    from potbot.controller.browser_cogs import BrowserControlService
    from potbot.controller.execution_cogs import HostOperation
    from potbot.semantics.embers import EmberContractBundle


def _signature(operation: str, input_type: str) -> CogSignature:
    return CogSignature(
        SynRef(f"syn://cog/potbot-browser-{operation}-v1"),
        f"syn://type/potbot-browser-{operation}-cog",
        input_type, EVIDENCE_RESULT, "potbot.cogs.execution.execute", 1, True,
    )


OBSERVE = _signature("observe", OBSERVE_REQUEST)
ACT = _signature("act", ACT_REQUEST)
PICK = _signature("pick", PICK_REQUEST)
CAPTURE = _signature("capture", CAPTURE_REQUEST)
BROWSER_COGS = (OBSERVE, ACT, PICK, CAPTURE)


def browser_cog_snapshot() -> SynSnapshot:
    from potbot.cogs.registry import _type

    base = execution_cog_snapshot()
    types, objects = list(base.types), list(base.objects)
    for ref, schema in schemas().items():
        types.append(replace(
            _type(ref, ref.rsplit("/", 1)[-1], "synny://type/potbot-data-record",
                  "Workspace-scoped browser command/evidence; never authorization"),
            metadata_json=canonical_json({
                "module": "potbot.cogs", "ontologyVersion": 1, "schema": schema,
            }),
        ))
    for signature in BROWSER_COGS:
        types.append(_type(
            signature.cog_type_ref, signature.cog_ref.slug, WORK_COG,
            "Typed browser operator on leased Xpra sandbox and existing evidence ledger",
        ))
        objects.append(SynObject(
            signature.cog_ref, parse_type_ref(signature.cog_type_ref), canonical_json({
                "id": str(signature.cog_ref), "typeRef": signature.cog_type_ref,
                "name": signature.cog_ref.slug, "schemaVersion": 1,
                "inputTypeRef": signature.input_type_ref,
                "outputTypeRef": signature.output_type_ref,
                "implementationBindingHash": signature.binding_hash,
                "handlerRef": signature.handler_ref,
                "implementationRevision": signature.implementation_revision,
                "requiresControllerScope": True,
            }),
        ))
    return replace(base, source_label="potbot-browser-cogs-v1",
                   types=tuple(types), objects=tuple(objects))


def browser_ember_bundle(*, workspace_id: UUID, snapshot_id: UUID,
                         namespace: str) -> EmberContractBundle:
    from potbot.cogs.tool_contracts import contract_ember_bundle

    return contract_ember_bundle(
        workspace_id=workspace_id, snapshot_id=snapshot_id, namespace=namespace,
        _snapshot=browser_cog_snapshot(), _installed=(*EXECUTION_COGS, *BROWSER_COGS),
        _cog_type=WORK_COG,
    )


def installed_operations(service: BrowserControlService, policy: Any) -> dict[CogSignature, HostOperation]:
    from potbot.controller.execution_cogs import HostOperation
    from potbot.tools.execution_contracts import WorkBinding

    operations: dict[CogSignature, HostOperation] = {}
    for signature, group in zip(BROWSER_COGS, ("observe", "act", "pick", "capture"), strict=True):

        async def handler(call: Any, source: TypedCogValue,
                          signature: CogSignature = signature, group: str = group) -> TypedCogValue:
            call._check()
            call.progress("browser_request_validated")
            call.runtime._admission(signature, source, call.work.context)
            session = source.value.get("session_id") if isinstance(source.value, dict) else None
            # The control RPC blocks on pipes. Never block the controller event loop,
            # and NEVER retry a timed-out/cancelled non-idempotent browser action.
            try:
                result = await asyncio.to_thread(
                    service.dispatch, group, source.value,
                    call.work.context.invocation_id, call.runtime.actor,
                )
            except asyncio.CancelledError as exc:
                if isinstance(session, str):
                    try:
                        service.fence(session)
                    except Exception:
                        pass  # Original cancellation is still indeterminate.
                raise IndeterminateExecutionError(
                    "browser operation cancelled; operator fenced its application session"
                ) from exc
            call._check()
            call.runtime._admission(signature, source, call.work.context)
            call.progress("browser_evidence_persisted")
            return TypedCogValue(signature.output_type_ref, result)

        operations[signature] = HostOperation(
            WorkBinding(
                "browser", f"syn://thing/{signature.cog_ref.slug}",
                signature.input_type_ref, signature.output_type_ref,
                signature.binding_hash,
                "read_only" if signature in (OBSERVE, CAPTURE) else "non_idempotent_write",
                policy,
            ),
            handler,
        )
    return operations
