"""Install admitted transports behind the existing monitored SYN work Cogs.

Ports and codecs are trusted host installations, never executable metadata from
an MCP server or model. Transport adapters retain their independent admission.
Every invocation returns through ExecutionCogRuntime and syn_execution_events.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from potbot.cogs.contracts import TypedCogValue
from potbot.controller.execution_cogs import ExecutionCall, HostOperation
from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
    ToolFailureError,
)
from potbot.domain.execution import ActionProposal
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.syn import parse_type_ref
from potbot.tools.api_contracts import ApiCallScope, ApiOperation
from potbot.tools.api_grpc import ApprovedGrpcAdapter
from potbot.tools.api_http import ApprovedHttpApiAdapter
from potbot.tools.capability_contracts import ToolInstallationDescriptor, ToolInvocationIntent
from potbot.tools.cli_adapter import ApprovedCliAdapter, CliObservation
from potbot.tools.execution_contracts import ExecutionPolicy, WorkBinding, digest
from potbot.tools.mcp_protocol import McpClient
from potbot.tools.mcp_sandbox import SandboxedMcpClient


@dataclass(frozen=True, slots=True)
class TransportPorts:
    """Source-pinned host conversion between canonical ports and native wire data."""

    input_type_ref: str
    output_type_ref: str
    codec_sha256: str
    encode: Callable[[dict[str, Any]], dict[str, Any]]
    decode: Callable[[Any], dict[str, Any]]

    def __post_init__(self) -> None:
        parse_type_ref(self.input_type_ref)
        parse_type_ref(self.output_type_ref)
        digest(self.codec_sha256)
        if not callable(self.encode) or not callable(self.decode):
            raise InvalidInputError("Transport conversion requires installed host code")

    def arguments(self, source: TypedCogValue) -> dict[str, Any]:
        if source.type_ref != self.input_type_ref:
            raise ScopeDeniedError("Transport input differs from its installed canonical port")
        value = self.encode(json.loads(canonical_json(source.value)))
        if not isinstance(value, dict) or len(canonical_json(value).encode()) > 16384:
            raise InvalidInputError("Transport arguments exceed their object/byte contract")
        copied = json.loads(canonical_json(value))
        assert isinstance(copied, dict)
        return copied

    def output(self, value: Any) -> TypedCogValue:
        result = self.decode(value)
        if not isinstance(result, dict):
            raise InvalidInputError("Transport decoder must return its canonical record")
        return TypedCogValue(self.output_type_ref, json.loads(canonical_json(result)))


def _binding(
    kind: str,
    ref: str,
    ports: TransportPorts,
    policy: ExecutionPolicy,
    identity: dict[str, Any],
    effect: str,
) -> WorkBinding:
    return WorkBinding(
        kind,
        ref,
        ports.input_type_ref,
        ports.output_type_ref,
        content_hash(
            {"adapter_revision": 1, "codec_sha256": ports.codec_sha256, "transport": identity}
        ),
        effect,
        policy,
    )


def api_operation(
    *,
    operation_ref: str,
    policy: ExecutionPolicy,
    ports: TransportPorts,
    adapter: ApprovedHttpApiAdapter | ApprovedGrpcAdapter,
    operation: ApiOperation,
    installation: ToolInstallationDescriptor,
    authorize: Callable[..., None],
    egress: Callable[[str], None] | None = None,
) -> HostOperation:
    """REST, GraphQL and gRPC retain their existing scope, schema and egress gates."""
    if not isinstance(adapter, (ApprovedHttpApiAdapter, ApprovedGrpcAdapter)) or not callable(
        authorize
    ):
        raise ScopeDeniedError("API Cog requires an installed admitted transport")
    if (
        operation.timeout_ms > policy.max_wall_ms
        or operation.max_output_bytes > policy.max_output_bytes
        or installation.policy_revision_hash != policy.policy_revision_hash
        or installation.configuration_hash != policy.configuration_hash
    ):
        raise ScopeDeniedError("API installation exceeds the pinned execution policy")
    if (isinstance(adapter, ApprovedGrpcAdapter)) != (operation.protocol == "grpc"):
        raise ScopeDeniedError("API operation belongs to another transport protocol")
    binding = _binding(
        "api",
        operation_ref,
        ports,
        policy,
        {
            "operation": operation.fingerprint,
            "installation": installation.fingerprint,
            "origin": adapter.origin if isinstance(adapter, ApprovedHttpApiAdapter) else None,
            "descriptor": adapter.codecs[operation.target].descriptor_hash
            if isinstance(adapter, ApprovedGrpcAdapter) and operation.target in adapter.codecs
            else None,
        },
        operation.effect_class,
    )

    async def invoke(call: ExecutionCall, value: TypedCogValue) -> TypedCogValue:
        call._check()
        if call.runtime.actor.workspace_id != installation.workspace_id:
            raise ScopeDeniedError("API installation belongs to another workspace")
        arguments = ports.arguments(value)
        intent = ToolInvocationIntent(
            invocation_id=call.work.context.invocation_id,
            workspace_id=installation.workspace_id,
            task_revision_id=call.work.context.task_revision_id,
            installation_hash=installation.fingerprint,
            input_type_ref=operation.input_type_ref,
            input_hash=content_hash(arguments),
            effect_class=operation.effect_class,
            policy_revision_hash=policy.policy_revision_hash,
            max_wall_time_ms=policy.max_wall_ms,
            max_output_bytes=policy.max_output_bytes,
            idempotency_key=str(call.work.context.invocation_id),
        )
        scope = ApiCallScope(installation, intent, operation.fingerprint, authorize, egress)
        call.progress("api_request_dispatch")
        result = await adapter.call(operation, arguments, scope)
        call.progress("api_response_received")
        result.to_observation(intent, operation)  # verifies operation and result hash
        if result.status == "indeterminate":
            raise IndeterminateExecutionError("API outcome is uncertain; inspect before retry")
        if result.status != "succeeded":
            raise ToolFailureError("API operation failed without a verified result")
        return ports.output(result.value)

    return HostOperation(binding, invoke)


def mcp_operation(
    *,
    operation_ref: str,
    policy: ExecutionPolicy,
    ports: TransportPorts,
    client: McpClient | SandboxedMcpClient,
    tool_name: str,
    catalog_sha256: str,
    tool_sha256: str,
    installation_sha256: str,
    effect_class: str,
    authorize: Callable[[ExecutionCall, dict[str, Any]], object],
) -> HostOperation:
    """Install exactly one already-discovered tool, with no discovery-based grant."""
    if not isinstance(client, (McpClient, SandboxedMcpClient)) or not callable(authorize):
        raise ScopeDeniedError("MCP Cog requires an installed client and separate admission")
    for pin in (catalog_sha256, tool_sha256, installation_sha256):
        digest(pin)
    if isinstance(client, SandboxedMcpClient) and effect_class != "read_only":
        raise ScopeDeniedError("Sandboxed MCP currently qualifies read-only operations")
    binding = _binding(
        "mcp_tool",
        operation_ref,
        ports,
        policy,
        {
            "installation": installation_sha256,
            "catalog": catalog_sha256,
            "tool": tool_sha256,
            "name": tool_name,
        },
        effect_class,
    )

    async def invoke(call: ExecutionCall, value: TypedCogValue) -> TypedCogValue:
        call._check()
        catalog = client.catalog
        if (
            catalog is None
            or catalog.fingerprint != catalog_sha256
            or catalog.tool(tool_name).fingerprint != tool_sha256
        ):
            raise StateMismatchError("MCP discovery changed; reinstall under current admission")
        arguments = ports.arguments(value)
        original_hash = content_hash(arguments)
        if authorize(call, arguments) is not None or content_hash(arguments) != original_hash:
            raise ScopeDeniedError("MCP admission must approve without modifying arguments")
        call.progress("mcp_tool_dispatch")
        if isinstance(client, SandboxedMcpClient):
            result = await client.call(tool_name, arguments)
        else:
            result = await client.call_tool(
                tool_name,
                arguments,
                pinned_catalog_hash=catalog_sha256,
                pinned_tool_hash=tool_sha256,
            )
        call.progress("mcp_tool_response_received")
        if result.is_error:
            raise ToolFailureError("MCP tool reported failure; content is not a successful result")
        if result.structured_content is None:
            raise InvalidInputError("This installed MCP codec requires structured output")
        return ports.output(result.structured_content)

    return HostOperation(binding, invoke)


def cli_operation(
    *,
    operation_ref: str,
    policy: ExecutionPolicy,
    ports: TransportPorts,
    adapter: ApprovedCliAdapter,
    tool_name: str,
) -> HostOperation:
    """Read-only CLI work uses the qualified supervisor, including cancellation."""
    if not isinstance(adapter, ApprovedCliAdapter):
        raise ScopeDeniedError("CLI Cog requires the approved sandbox adapter")
    definition = adapter.registry.get(tool_name)
    if (
        definition.mutates
        or definition.timeout_ms > policy.max_wall_ms
        or definition.max_output_bytes > policy.max_output_bytes
    ):
        raise ScopeDeniedError("CLI installation exceeds its read-only work policy")
    binding = _binding(
        "script",
        operation_ref,
        ports,
        policy,
        {
            "definition": definition.fingerprint,
            "executable": adapter.pinned_executables.get(definition.argv_prefix[0]),
            "sandbox": str(adapter.sandbox.root),
            "candidate": str(adapter.candidate_root),
        },
        "read_only",
    )

    async def invoke(call: ExecutionCall, value: TypedCogValue) -> TypedCogValue:
        call._check()
        proposal = ActionProposal.from_dict(ports.arguments(value))
        if (
            proposal.kind != tool_name
            or adapter.registry.get(tool_name).fingerprint != definition.fingerprint
        ):
            raise ScopeDeniedError("CLI proposal differs from its installed tool")
        cancel = threading.Event()
        call.progress("cli_supervisor_dispatch")
        task = asyncio.create_task(
            asyncio.to_thread(adapter.run, proposal, cancelled=cancel.is_set)
        )
        try:
            result: CliObservation = await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancel.set()
            # Do not emit terminal cancellation while the owned process still runs.
            stopped = await asyncio.shield(task)
            if not stopped.processes_stopped:
                raise IndeterminateExecutionError("Cancelled CLI cleanup is unverified") from error
            raise
        call.progress("cli_process_tree_closed")
        if not result.processes_stopped:
            raise IndeterminateExecutionError("CLI process cleanup is unverified")
        if result.exit_code != 0 or result.outcome != "completed":
            raise ToolFailureError("CLI process failed; inspect its bounded observation")
        return ports.output(result)

    return HostOperation(binding, invoke)
