"""Installed language-neutral debugger Cog definitions and controller-only handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from potbot.cogs.contracts import CogSignature, TypedCogValue
from potbot.domain.serialization import canonical_json
from potbot.domain.syn import SynObject, SynProperty, SynRef, SynSnapshot, parse_type_ref

if TYPE_CHECKING:
    from potbot.debugging.cog_runtime import DebuggerCogRuntime

TOOL_COG = "syn://type/potbot-debug-tool-cog"
OPERATIONS = (
    "initialize",
    "launch",
    "attach",
    "set_breakpoints",
    "configure_exceptions",
    "configuration_done",
    "wait_stopped",
    "threads",
    "stack",
    "scopes",
    "variables",
    "step_over",
    "step_into",
    "step_out",
    "continue",
    "pause",
    "exception_info",
    "wait_terminated",
    "detach",
    "terminate",
    "evaluate",
)


def cog_ref(operation: str) -> str:
    return "syn://cog/potbot-debug-" + operation.replace("_", "-") + "-v1"


def signature(operation: str) -> CogSignature:
    stem = "syn://type/potbot-debug-" + operation.replace("_", "-")
    return CogSignature(
        SynRef(cog_ref(operation)),
        stem + "-cog",
        stem + "-request",
        stem + "-result",
        "potbot.cogs.debugger.execute",
        1,
        True,
    )


DEBUGGER_COGS = tuple(signature(operation) for operation in OPERATIONS)


def debugger_cog_snapshot() -> SynSnapshot:
    # The same registry/catalog contains pure and execution Cogs. No second registry.
    from potbot.cogs.registry import _type, builtin_cog_snapshot

    base = builtin_cog_snapshot()
    fields = (
        SynProperty("inputTypeRef", SynRef("syn://type/type")),
        SynProperty("outputTypeRef", SynRef("syn://type/type")),
        SynProperty(
            "implementationBindingHash", parse_type_ref("synny://type/potbot-data-value-string")
        ),
    )
    definitions = [
        _type(
            TOOL_COG,
            "Debugger execution Cog",
            "syn://type/cog",
            "Controller-admitted debugger request to typed result; data confers no authority.",
            fields,
        )
    ]
    objects = list(base.objects)
    for operation, installed in zip(OPERATIONS, DEBUGGER_COGS, strict=True):
        definitions.extend(
            (
                _type(
                    installed.cog_type_ref,
                    f"Debugger {operation}",
                    TOOL_COG,
                    "Installed async handler with mandatory controller authority and work monitoring.",
                ),
                _type(
                    installed.input_type_ref,
                    f"Debugger {operation} request",
                    "synny://type/potbot-data-record",
                    "One versioned language-neutral request envelope; opaque handles grant no authority.",
                ),
                _type(
                    installed.output_type_ref,
                    f"Debugger {operation} result",
                    "synny://type/potbot-data-record",
                    "One bounded normalized result; completion and verification remain distinct.",
                ),
            )
        )
        objects.append(
            SynObject(
                ref=installed.cog_ref,
                type_ref=parse_type_ref(installed.cog_type_ref),
                payload_json=canonical_json(
                    {
                        "id": str(installed.cog_ref),
                        "name": operation,
                        "typeRef": installed.cog_type_ref,
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
        source_label="potbot-debugger-cogs-v1",
        catalog_mode="extend_builtin",
        types=(*base.types, *definitions),
        objects=tuple(objects),
    )


async def execute(
    runtime: DebuggerCogRuntime, operation: str, source: TypedCogValue
) -> dict[str, Any]:
    """Only the bound registry calls this under its controller's admitted work run."""
    return await runtime.dispatch(operation, source.value)
