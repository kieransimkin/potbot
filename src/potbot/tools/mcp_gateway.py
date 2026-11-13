"""Controller admission and durable completion for one read-only MCP tool call."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.controller.execution import scoped_row
from potbot.controller.tasks import lock_episode
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.execution import ActionProposal, ToolDefinition
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.evidence import append_event
from potbot.tools.capability_contracts import (
    ToolCapabilityDescriptor,
    ToolImplementationDescriptor,
    ToolInstallationDescriptor,
    ToolInvocationIntent,
)
from potbot.tools.interactive_supervisor import InteractiveProcessSupervisor
from potbot.tools.mcp_confirmation import (
    McpConfirmation,
    McpConfirmationDeclined,
    McpConfirmationPrompt,
)
from potbot.tools.mcp_http_worker import check_http_worker
from potbot.tools.mcp_protocol import (
    MAX_WIRE_BYTES,
    TOOLS_LISTEN,
    McpCallResult,
    McpToolInfo,
    validate_arguments,
)
from potbot.tools.mcp_sandbox import ManagedMcpScope, ReadOnlyMcpGrant, SandboxedMcpClient
from potbot.tools.repository import Lease, assert_lease
from potbot.workspace_sandbox import WorkspaceSandbox

if TYPE_CHECKING:
    from potbot.tools.gateway import ToolGateway
    from potbot.tools.mcp_oauth import McpOAuthProvider


def mcp_configuration_hash(
    definition: ToolDefinition,
    tool: McpToolInfo,
    executable: str,
    *,
    http_provider: McpOAuthProvider | None = None,
    confirmation: McpConfirmation | None = None,
    watch_ms: int = 0,
) -> str:
    identity: dict[str, str | int] = {
        "definition": definition.fingerprint,
        "tool": tool.fingerprint,
        "executable_sha256": executable,
    }
    if http_provider is not None:
        identity["oauth_registration_sha256"] = http_provider.registration_fingerprint
    if confirmation is not None:
        identity["confirmation_field"] = confirmation.field
    if watch_ms:
        identity["catalog_watch_ms"] = watch_ms
    return content_hash(identity)


@dataclass(frozen=True, slots=True)
class InstalledMcpTool:
    """Operator-owned selection, independent of any model action or advertised grant."""

    definition: ToolDefinition
    tool: McpToolInfo
    executable_sha256: str
    capability: ToolCapabilityDescriptor
    implementation: ToolImplementationDescriptor
    installation: ToolInstallationDescriptor
    http_provider: McpOAuthProvider | None = field(default=None, repr=False)
    confirmation: McpConfirmation | None = field(default=None, repr=False)
    watch_ms: int = 0

    def __post_init__(self) -> None:
        if (
            self.definition.protocol != "mcp"
            or self.capability.protocol != "mcp"
            or self.capability.effect_class != "read_only"
            or self.capability.input_schema_json != self.tool.input_schema_json
            or self.capability.output_schema_json
            != (self.tool.output_schema_json or canonical_json({"type": "object"}))
            or self.definition.capability not in self.installation.permitted_scopes
            or re.fullmatch(r"[a-f0-9]{64}", self.executable_sha256) is None
        ):
            raise ScopeDeniedError("MCP installation differs from the operator's tool and scope")
        if self.tool.header_parameters and self.http_provider is None:
            raise ScopeDeniedError("MCP header parameters require an approved HTTP installation")
        check_http_worker(
            self.definition.argv_prefix[1:],
            http=self.http_provider is not None,
            confirmation=self.confirmation is not None,
            watch_ms=self.watch_ms,
        )
        if self.confirmation is not None and (
            self.confirmation.tool_fingerprint != self.tool.fingerprint
            or self.confirmation.capability.fingerprint != self.capability.fingerprint
        ):
            raise ScopeDeniedError(
                "MCP confirmation differs from its installed tool and capability"
            )
        if (
            self.http_provider is not None
            and self.http_provider.grant.workspace_id != self.installation.workspace_id
        ):
            raise ScopeDeniedError("MCP HTTP installation differs from its registered workspace")
        self.implementation.assert_capability(self.capability)
        self.installation.assert_implementation(self.implementation)
        if self.installation.configuration_hash != mcp_configuration_hash(
            self.definition,
            self.tool,
            self.executable_sha256,
            http_provider=self.http_provider,
            confirmation=self.confirmation,
            watch_ms=self.watch_ms,
        ):
            raise ScopeDeniedError("MCP executable, configuration, or tool identity changed")


def _frozen_input(raw: bytes, installation: ToolInstallationDescriptor) -> dict[str, Any]:
    if not raw or len(raw) > MAX_WIRE_BYTES:
        raise InvalidInputError("MCP action input exceeds its bound")
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise InvalidInputError("MCP action input is invalid JSON") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"installation_hash", "arguments"}
        or not isinstance(value["arguments"], dict)
        or canonical_json(value).encode("utf-8") != raw
    ):
        raise InvalidInputError("MCP action requires canonical arguments and an installation hash")
    if value["installation_hash"] != installation.fingerprint:
        raise ScopeDeniedError("MCP action does not select the installed policy and tool")
    return value["arguments"]


def _record_mcp_event(
    gateway: ToolGateway,
    context: WorkspaceContext,
    run: dict[str, Any],
    lease: Lease,
    event_type: str,
    details: dict[str, Any],
    *,
    watch: bool = False,
) -> None:
    """Commit the next step before returning to the provider dispatch path."""
    with gateway.database.transaction(context) as conn:
        episode = lock_episode(conn, context, run["episode_id"])
        assert_lease(conn, context, lease)
        current = scoped_row(conn, context, "action_runs", run["id"])
        reservation = scoped_row(conn, context, "budget_reservations", run["reservation_id"])
        if (
            episode["status"] != "running"
            or episode["active_contract_id"] != run["contract_revision_id"]
            or current["status"] != "running"
            or current["owner_instance"] != gateway.instance_id
            or reservation["status"] != "reserved"
            or reservation["amounts"].get("calls", 0) < 2
        ):
            raise ScopeDeniedError("MCP exchange lost its durable admission")
        rows = conn.execute(
            "SELECT event_type,details FROM evidence_events WHERE workspace_id=%s AND run_id=%s "
            "AND event_type IN ('mcp_confirmation_decision','mcp_http_dispatch','mcp_catalog_observed')",
            (context.workspace_id, run["id"]),
        ).fetchall()
        step = (event_type, details.get("round"))
        allowed = (
            (not rows and step == ("mcp_http_dispatch", 1))
            or (
                len(rows) == 1
                and rows[0]["event_type"] == "mcp_http_dispatch"
                and rows[0]["details"]["round"] == 1
                and step == ("mcp_confirmation_decision", None)
            )
            or (
                len(rows) == 2
                and any(
                    row["event_type"] == "mcp_confirmation_decision"
                    and row["details"]["approved"] is True
                    for row in rows
                )
                and step == ("mcp_http_dispatch", 2)
            )
        )
        if watch:
            observed = {row["event_type"] for row in rows}
            allowed = (
                (
                    not rows
                    and step == ("mcp_http_dispatch", 1)
                    and details.get("method") == TOOLS_LISTEN
                )
                or (
                    observed == {"mcp_http_dispatch"}
                    and len(rows) == 1
                    and event_type == "mcp_catalog_observed"
                )
                or (
                    observed == {"mcp_http_dispatch", "mcp_catalog_observed"}
                    and len(rows) == 2
                    and step == ("mcp_http_dispatch", 2)
                    and details.get("method") == "tools/call"
                )
            )
        if not allowed:
            raise ScopeDeniedError("MCP dispatch is out of sequence")
        append_event(
            conn,
            context,
            event_type,
            episode_id=run["episode_id"],
            run_id=run["id"],
            details=details,
        )


async def execute_mcp_action(
    gateway: ToolGateway,
    context: WorkspaceContext,
    action_id: UUID,
    lease: Lease,
    reservation_id: UUID,
    *,
    after_receipt: Callable[[], None] | None,
) -> dict[str, Any]:
    prior = gateway.store.inspect(context, action_id)
    definition = gateway.registry.get(prior["action"]["kind"])
    if definition.protocol != "mcp":
        raise ScopeDeniedError("This action does not select an installed MCP tool")
    if prior["run"] is not None:
        return gateway._replay(context, prior, action_id, lease, reservation_id)

    binding = gateway._mcp_installations.get((context.workspace_id, definition.name))
    syn_route = gateway._mcp_routes.get(context.workspace_id)
    if (
        binding is None
        or syn_route is None
        or gateway.shared_runtime is None
        or gateway.agent_homes is None
        or gateway.host_agent_id is None
    ):
        raise ScopeDeniedError(
            "MCP requires an operator installation, Syn route, and managed Agent"
        )
    if (
        binding.http_provider is not None
        and binding.http_provider.grant.principal_id != context.principal_id
    ):
        raise ScopeDeniedError("MCP HTTP installation belongs to another principal")
    input_id = prior["action"].get("stdin_artifact_id")
    if input_id is None:
        raise InvalidInputError("MCP action requires a frozen input artifact")
    arguments = _frozen_input(gateway.artifacts.read(context, UUID(input_id)), binding.installation)
    validate_arguments(binding.tool, arguments)

    candidate = gateway.candidates.check_lease(context, lease)
    agent = gateway.agent_homes.lookup(context.workspace_id, gateway.host_agent_id)
    if agent is None:
        raise ScopeDeniedError("The controller's MCP Agent home is unavailable")
    sandbox = WorkspaceSandbox.existing(gateway.shared_runtime.home / agent.directory)
    root = sandbox.confine(candidate["path"], area="candidates", must_exist=True)
    expected = (
        sandbox.path("candidates") / str(context.workspace_id) / str(lease.candidate_id) / "tree"
    )
    if root != expected or gateway.candidates.root != sandbox.path("candidates"):
        raise ScopeDeniedError("MCP action must use one managed Agent candidate")
    runtime = gateway.shared_runtime
    mounts = (
        runtime.home,
        runtime.rootfs,
        runtime.shared_tools,
        runtime.model_staging,
        runtime.shared_models,
        runtime.shared_skills,
        runtime.ai_config_dir,
        runtime.ai_history_dir,
    )
    for private in (gateway.receipts.root, getattr(gateway.artifacts.backend, "root", None)):
        if isinstance(private, Path) and any(
            private.resolve().is_relative_to(mount.resolve())
            or mount.resolve().is_relative_to(private.resolve())
            for mount in mounts
            if mount is not None
        ):
            raise ScopeDeniedError("MCP worker mounts overlap private controller storage")

    confirmation = binding.confirmation
    watch_ms = binding.watch_ms
    journaled = bool(confirmation or watch_ms)
    intent = None
    task_contract = None

    def pin_intent(
        action: dict[str, Any],
        contract: dict[str, Any],
        selected: ToolDefinition,
        proposal: ActionProposal,
        run_id: UUID,
    ) -> dict[str, Any]:
        nonlocal intent, task_contract
        if (
            str(action["stdin_artifact_id"]) != input_id
            or selected.fingerprint != binding.definition.fingerprint
            or proposal.working_directory != "."
            or proposal.output_paths
        ):
            raise ScopeDeniedError("MCP admission differs from the frozen action and installation")
        intent = ToolInvocationIntent(
            invocation_id=run_id,
            workspace_id=context.workspace_id,
            task_revision_id=contract["id"],
            installation_hash=binding.installation.fingerprint,
            input_type_ref=binding.capability.input_type_ref,
            input_hash=content_hash(arguments),
            effect_class="read_only",
            policy_revision_hash=binding.installation.policy_revision_hash,
            max_wall_time_ms=selected.timeout_ms,
            max_output_bytes=selected.max_output_bytes,
            idempotency_key=action_id.hex,
        )
        intent.assert_capability(binding.capability)
        intent.assert_installation(binding.installation)
        task_contract = contract["content"]
        details: dict[str, Any] = {}
        if confirmation:
            details["mcp_confirmation_field"] = confirmation.field
        if watch_ms:
            details["mcp_catalog_watch_ms"] = watch_ms
        return {
            **details,
            "mcp_installation": binding.installation.to_dict(),
            "mcp_intent": intent.to_dict(),
            "mcp_intent_hash": intent.fingerprint,
        }

    run, _, _, candidate = gateway._admit(
        context,
        action_id,
        lease,
        reservation_id,
        min_calls=2 if journaled else 1,
        pin_intent=pin_intent,
    )
    assert intent is not None and task_contract is not None
    started = monotonic()

    def cancelled() -> bool:
        return gateway._cancelled(context, run, lease, require_reservation=True)

    def approve(tool: McpToolInfo, supplied: Mapping[str, Any]) -> None:
        if (
            tool.fingerprint != binding.tool.fingerprint
            or content_hash(dict(supplied)) != intent.input_hash
            or cancelled()
        ):
            raise ScopeDeniedError("MCP call differs from its live durable admission")

    calls = 0

    async def confirm(prompt: McpConfirmationPrompt) -> bool:
        assert confirmation is not None
        if cancelled():
            raise ScopeDeniedError("MCP confirmation admission expired")
        approved = await confirmation.approve(prompt) is True
        _record_mcp_event(
            gateway,
            context,
            run,
            lease,
            "mcp_confirmation_decision",
            {
                "principal_id": str(context.principal_id),
                "field": prompt.field,
                "prompt_sha256": content_hash({"tool": prompt.tool, "message": prompt.message}),
                "approved": approved,
            },
        )
        return approved

    async def record_call(request: dict[str, Any]) -> None:
        nonlocal calls
        details = {"round": calls + 1, "request_sha256": content_hash(request)}
        if watch_ms:
            details["method"] = request["method"]
        _record_mcp_event(
            gateway, context, run, lease, "mcp_http_dispatch", details, watch=bool(watch_ms)
        )
        calls += 1

    try:
        if Path(candidate["path"]).resolve(strict=True) != root:
            raise StateMismatchError("MCP candidate changed during admission")
        async with SandboxedMcpClient(
            supervisor=InteractiveProcessSupervisor(
                gateway.supervisor.backend, monitor_config=gateway.supervisor.monitor_config
            ),
            sandbox=sandbox,
            candidate_root=Path(candidate["path"]),
            server_kind=definition.name,
            registry=gateway.registry,
            capabilities=gateway.capabilities,
            task_contract=task_contract,
            executable_sha256=binding.executable_sha256,
            permitted=(ReadOnlyMcpGrant(binding.tool.name, binding.tool.fingerprint),),
            approve_call=approve,
            cancelled=cancelled,
            syn_route=syn_route,
            workspace_id=context.workspace_id,
            http_provider=binding.http_provider,
            http_header_tool=binding.tool if binding.tool.header_parameters else None,
            confirmation=replace(confirmation, approve=confirm) if confirmation else None,
            before_http_call=record_call if journaled else None,
            watch_ms=watch_ms,
            managed_scope=ManagedMcpScope(
                context, gateway.host_agent_id, run["id"], gateway.agent_homes
            ),
        ) as client:
            blocked = None
            if watch_ms:
                changed = await client.watch_tool_catalog()
                refreshed = client.catalog
                _record_mcp_event(
                    gateway,
                    context,
                    run,
                    lease,
                    "mcp_catalog_observed",
                    {"changed": changed, "catalog_sha256": refreshed.fingerprint},
                    watch=True,
                )
                if not any(
                    tool.fingerprint == binding.tool.fingerprint for tool in refreshed.tools
                ):
                    blocked = "mcp_catalog_changed"
            try:
                result = (
                    McpCallResult.from_wire(binding.tool.name, {"isError": True})
                    if blocked
                    else await client.call(binding.tool.name, arguments)
                )
            except McpConfirmationDeclined:
                if confirmation is None:
                    raise
                result = McpCallResult.from_wire(binding.tool.name, {"isError": True})
                blocked = "mcp_confirmation_declined"
            exit_receipt = await client.close()
        if exit_receipt is None or not exit_receipt.processes_stopped or cancelled():
            raise IndeterminateExecutionError("MCP shutdown or controller admission is unconfirmed")
        if binding.http_provider is not None and exit_receipt.return_code != 0:
            raise IndeterminateExecutionError("MCP broker relay did not exit successfully")
        if gateway._fingerprint(context, candidate) != run["input_fingerprint"]:
            raise StateMismatchError("Read-only MCP action changed the candidate contents")
        observation = result.to_observation(intent=intent, capability=binding.capability)
        if blocked:
            observation = replace(observation, error_code=blocked)
        stdout = canonical_json(
            {
                "observation": observation.to_dict(),
                "content": list(result.content),
                "structured_content": result.structured_content,
            }
        ).encode()
        if len(stdout) > definition.max_output_bytes:
            raise StateMismatchError("MCP observation exceeds the admitted result size")
        receipt = {
            "schema_version": 1,
            "workspace_id": str(context.workspace_id),
            "run_id": str(run["id"]),
            "action_id": str(action_id),
            "candidate_id": str(lease.candidate_id),
            "lease_owner": lease.owner,
            "lease_generation": lease.generation,
            "input_fingerprint": run["input_fingerprint"],
            "result_fingerprint": run["input_fingerprint"],
            "outcome": "inconclusive" if result.is_error else "completed",
            "exit_code": exit_receipt.return_code,
            "elapsed_ms": int((monotonic() - started) * 1000),
            "stdout": base64.b64encode(stdout).decode("ascii"),
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": exit_receipt.stderr_truncated,
            "dispatched": True,
            "isolation_identity": exit_receipt.isolation_identity,
            "error_code": observation.error_code,
            "retryable": False,
            "processes_stopped": True,
            "stdin_artifact_id": input_id,
            "stdin_digest": content_hash(
                {"installation_hash": binding.installation.fingerprint, "arguments": arguments}
            ),
            "mcp_intent_hash": intent.fingerprint,
        }
        if journaled:
            receipt["mcp_http_calls"] = calls
        gateway.receipts.write(context.workspace_id, run["id"], receipt)
        if after_receipt is not None:
            after_receipt()
        gateway._complete(context, run, lease, receipt)
        return gateway.store.inspect(context, action_id)
    except BaseException:
        gateway._uncertain(context, run, lease)
        raise
