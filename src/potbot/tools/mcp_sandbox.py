"""T02.3: controller-approved MCP stdio server on a qualified local sandbox.

This bridges T02.1's *existing* McpClient/McpStdioTransport to Potbot's
Bubblewrap-backed ProcessSupervisor, WorkspaceSandbox and ToolRegistry.
No model-provided executable or environment is executed. Server tool metadata
is not trusted to grant access. This offline/local adapter deliberately disables
external network and rejects credentials until T03's scoped durable broker exists.

DO NOT expose this as a standalone authorization API: a controller must first
obtain and persist the existing task/lease/budget approval, and must pass the
trusted per-call approval callback. All effects are limited to the operator's
pre-approved read-only tool names in this initial adapter.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.execution import ActionProposal, CapabilityContext, ToolDefinition, ToolRegistry
from potbot.domain.serialization import content_hash
from potbot.tools.agent_homes import AgentHomeRegistry
from potbot.tools.interactive_supervisor import (
    InteractiveExit,
    InteractiveProcessSupervisor,
    IsolatedInteractiveProcess,
)
from potbot.tools.mcp_protocol import McpCallResult, McpCatalog, McpClient, McpToolInfo
from potbot.tools.mcp_stdio import McpStdioTransport
from potbot.tools.shared_workspace_isolation import SharedWorkspaceBubblewrapBackend
from potbot.tools.supervisor import ProcessRequest
from potbot.tools.syn_mcp_route import SynMcpRoute
from potbot.tools.worker_telemetry import WorkerMonitorConfig
from potbot.tools.workspace_supervision import (
    WorkspaceExecutorIdentity,
    WorkspaceLeaseGuard,
    assert_shared_execution_allowed,
)
from potbot.workspace_sandbox import WorkspaceSandbox

# A function installed by the trusted controller that throws if the selected
# tool/arguments are not covered by the active task's durable permission grant.
CallAdmission = Callable[[McpToolInfo, Mapping[str, Any]], None]


@dataclass(frozen=True, slots=True)
class ReadOnlyMcpGrant:
    """Operator-curated capability allowance. Not obtained from MCP annotations."""

    tool_name: str
    tool_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, str) or not 1 <= len(self.tool_name) <= 128:
            raise InvalidInputError("MCP allowed tool name is invalid")
        if self.tool_fingerprint is not None and (
            len(self.tool_fingerprint) != 64
            or any(x not in "0123456789abcdef" for x in self.tool_fingerprint)
        ):
            raise InvalidInputError("MCP pinned tool fingerprint must be SHA-256")


@dataclass(frozen=True, slots=True)
class ManagedMcpScope:
    """Controller-provided run identity for one installed Agent home."""

    principal: WorkspaceContext
    agent_id: UUID
    run_id: UUID
    homes: AgentHomeRegistry

    def __post_init__(self) -> None:
        if (
            not isinstance(self.principal, WorkspaceContext)
            or not isinstance(self.agent_id, UUID)
            or not isinstance(self.run_id, UUID)
            or not isinstance(self.homes, AgentHomeRegistry)
        ):
            raise ScopeDeniedError("Managed MCP requires a registered controller run")


class SandboxedMcpClient:
    """An explicit controller-owned MCP process; no implicit child launches."""

    def __init__(
        self,
        *,
        supervisor: InteractiveProcessSupervisor,
        sandbox: WorkspaceSandbox,
        candidate_root: Path,
        server_kind: str,
        registry: ToolRegistry,
        capabilities: CapabilityContext,
        task_contract: Mapping[str, Any],
        executable_sha256: str,
        permitted: tuple[ReadOnlyMcpGrant, ...],
        approve_call: CallAdmission,
        cancelled: Callable[[], bool] | None = None,
        syn_route: SynMcpRoute | None = None,
        workspace_id: UUID | None = None,
        managed_scope: ManagedMcpScope | None = None,
    ) -> None:
        if not isinstance(supervisor, InteractiveProcessSupervisor):
            raise InvalidInputError("MCP requires the qualified interactive ProcessSupervisor")
        if not isinstance(sandbox, WorkspaceSandbox):
            raise InvalidInputError("MCP requires a registered WorkspaceSandbox")
        shared = isinstance(supervisor.backend, SharedWorkspaceBubblewrapBackend)
        if shared != (managed_scope is not None):
            raise ScopeDeniedError("Shared MCP execution requires a managed Agent scope")
        if managed_scope is not None and (
            not isinstance(managed_scope, ManagedMcpScope)
            or workspace_id != managed_scope.principal.workspace_id
            or syn_route is None
        ):
            raise ScopeDeniedError("Managed MCP requires a Syn route for the admitted workspace")
        if syn_route is not None and (
            not isinstance(syn_route, SynMcpRoute)
            or not isinstance(workspace_id, UUID)
            or syn_route.workspace_id != workspace_id
        ):
            raise ScopeDeniedError("The Syn MCP route must match the controller workspace")
        root = sandbox.confine(candidate_root, area="candidates", must_exist=True)
        if not root.is_dir() or root.parent != sandbox.path("candidates"):
            raise ScopeDeniedError("MCP must run inside one candidate directory")
        if (
            not isinstance(executable_sha256, str)
            or len(executable_sha256) != 64
            or any(x not in "0123456789abcdef" for x in executable_sha256)
        ):
            raise InvalidInputError("MCP server executable requires a pinned SHA-256")
        if not callable(approve_call):
            raise ScopeDeniedError("MCP tool calls require a controller-owned admission function")
        if (
            not permitted
            or len(permitted) > 256
            or any(not isinstance(x, ReadOnlyMcpGrant) for x in permitted)
            or len(set(x.tool_name for x in permitted)) != len(permitted)
        ):
            raise InvalidInputError("MCP approved tool list must be finite and unique")
        self._supervisor = supervisor
        self._sandbox = sandbox
        self._root = root
        self._server_kind = server_kind
        self._registry = registry
        self._capabilities = capabilities
        self._task_contract = task_contract
        self._executable_hash = executable_sha256
        self._grants = {item.tool_name: item for item in permitted}
        self._approve_call = approve_call
        self._cancelled = cancelled
        self._syn_route = syn_route
        self._managed_scope = managed_scope
        self._lease: WorkspaceLeaseGuard | None = None
        self._call_lock = asyncio.Lock()
        self._process: IsolatedInteractiveProcess | None = None
        self._transport: McpStdioTransport | None = None
        self._client: McpClient | None = None
        self._catalog: McpCatalog | None = None
        self._launched_definition_hash: str | None = None

    @property
    def catalog(self) -> McpCatalog:
        if self._catalog is None:
            raise StateMismatchError("MCP server has not completed discovery")
        return self._catalog

    @property
    def isolation_identity(self) -> str | None:
        return self._process.isolation_identity if self._process is not None else None

    def _admit(self, tool: McpToolInfo, arguments: Mapping[str, Any]) -> None:
        allowed = self._grants.get(tool.name)
        if allowed is None:
            raise ScopeDeniedError("MCP tool is not on the controller's exact allowlist")
        if allowed.tool_fingerprint is not None and tool.fingerprint != allowed.tool_fingerprint:
            raise StateMismatchError("MCP tool signature changed since operator approval")
        decision = self._approve_call(tool, arguments)
        if decision is not None:
            raise ScopeDeniedError(
                "MCP approval callback must fail by raising, not by returning a value"
            )

    def _managed_request(self, definition: ToolDefinition) -> tuple[ProcessRequest, Path]:
        scope, backend = self._managed_scope, self._supervisor.backend
        assert scope is not None and isinstance(backend, SharedWorkspaceBubblewrapBackend)
        config = self._supervisor.monitor_config or WorkerMonitorConfig.from_environment(
            scoped_worker=True
        )
        if (
            config.scoped_launcher is None
            or not config.scoped_launcher.is_socket()
            or backend.tool_write_grant is not None
        ):
            raise ScopeDeniedError("Managed MCP requires the read-only scoped launcher profile")
        runtime, workspace = backend.runtime, scope.principal.workspace_id
        agent_home = scope.homes.lookup(workspace, scope.agent_id)
        if agent_home is None or self._sandbox.root != (
            runtime.home / agent_home.directory
        ).resolve(strict=True):
            raise ScopeDeniedError("MCP candidate must belong to the registered Agent home")
        assert_shared_execution_allowed(
            runtime=runtime,
            ledger=scope.homes.ledger,
            workspace_id=workspace,
            candidate_path=str(self._root),
        )
        entrypoint = definition.argv_prefix[0]
        if entrypoint.startswith("/usr/local/tools/"):
            base = runtime.shared_tools
            relative = entrypoint.removeprefix("/usr/local/tools/")
            if (
                base is None
                or dict(runtime.tool_entrypoints).get(entrypoint) != self._executable_hash
            ):
                raise ScopeDeniedError("MCP shared entrypoint is not pinned by the operator")
        else:
            base, relative = runtime.rootfs, entrypoint.removeprefix("/")
        executable = (base / relative).resolve(strict=True)
        if not executable.is_relative_to(base.resolve(strict=True)):
            raise ScopeDeniedError("MCP executable escapes the admitted worker image or tool store")
        request = ProcessRequest(
            argv=definition.argv_prefix,
            cwd=self._root,
            workspace_root=runtime.home,
            sandbox_root=runtime.home,
            environment=dict(definition.environment),
            timeout_ms=definition.timeout_ms,
            max_output_bytes=definition.max_output_bytes,
            guest_uid=agent_home.uid,
            guest_gid=agent_home.gid,
            telemetry_workspace_id=workspace,
            telemetry_agent_id=scope.agent_id,
            telemetry_run_id=scope.run_id,
            telemetry_identity_kind="agent",
        )
        return request, executable

    def _cancel_requested(self) -> bool:
        return (self._lease is not None and self._lease.fenced) or (
            self._cancelled is not None and self._cancelled()
        )

    async def _open_managed(self, request: ProcessRequest) -> None:
        scope, backend = self._managed_scope, self._supervisor.backend
        assert scope is not None and isinstance(backend, SharedWorkspaceBubblewrapBackend)
        guard = WorkspaceLeaseGuard.exclusive_home(
            scope.homes.ledger,
            WorkspaceExecutorIdentity(
                scope.principal.workspace_id,
                scope.agent_id,
                backend.runtime.image_digest,
            ),
            operation="mcp.sandbox.execute",
            idempotency_key="mcp-" + scope.run_id.hex,
        )
        guard.start()
        self._lease = guard
        opening = asyncio.create_task(
            self._supervisor.open(request, cancelled=self._cancel_requested)
        )
        try:
            self._process = await asyncio.shield(opening)
        except BaseException:
            # A cancelled caller still owns any process the launcher creates.
            while not opening.done():
                try:
                    await asyncio.shield(opening)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not opening.cancelled() and opening.exception() is None:
                self._process = opening.result()
                await self.close()
            else:
                self._finish_lease(None)
            raise

    def _finish_lease(self, result: InteractiveExit | None) -> None:
        guard = self._lease
        if guard is None:
            return
        self._lease = None
        try:
            guard.finish(
                evidence_sha256=content_hash(
                    {
                        "isolation": result.isolation_identity if result else None,
                        "exit_code": result.return_code if result else None,
                        "status_sha256": result.status_sha256 if result else None,
                    }
                ),
                uncertain=result is None or not result.processes_stopped or guard.fenced,
            )
        finally:
            guard.abandon()

    async def __aenter__(self) -> SandboxedMcpClient:
        if self._process is not None:
            raise StateMismatchError("An MCP server lifetime cannot be restarted")
        if os.environ.get("POTBOT_OPERATOR_REQUIRED") == "1":
            if self._syn_route is None:
                raise ScopeDeniedError("Managed MCP execution requires its installed Syn route")
            if self._managed_scope is None:
                raise ScopeDeniedError("Managed MCP execution requires a scoped Agent launch")
        definition = self._registry.get(self._server_kind)
        definition.admit(ActionProposal(kind=self._server_kind))
        self._capabilities.authorize(definition, self._task_contract)
        if definition.secret_refs:
            raise ScopeDeniedError("MCP secret injection awaits a qualified credential broker")
        if any(
            name not in {"LANG", "LC_ALL", "TZ", "PYTHONUNBUFFERED", "NO_COLOR"}
            for name, _ in definition.environment
        ):
            raise ScopeDeniedError("MCP server environment variable is not approved")
        if not definition.accepts_stdin or definition.allow_arguments:
            raise ScopeDeniedError("MCP server must have fixed argv and approved stdio")
        if definition.mutates:
            raise ScopeDeniedError("MCP Phase 2 permits explicitly read-only server installations")
        if self._managed_scope is not None:
            request, executable = self._managed_request(definition)
        else:
            executable = Path(definition.argv_prefix[0])
            request = ProcessRequest(
                argv=definition.argv_prefix,
                cwd=self._root,
                workspace_root=self._root,
                sandbox_root=self._sandbox.root,
                environment={**dict(definition.environment), **self._sandbox.runtime_environment()},
                timeout_ms=definition.timeout_ms,
                max_output_bytes=definition.max_output_bytes,
                read_only_roots=tuple(
                    self._sandbox.path(area) for area in ("models", "tools", "dependencies")
                ),
                writable_roots=tuple(
                    self._sandbox.path(area)
                    for area in ("tmp", "cache", "environments", "builds", "tests")
                ),
            )
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ScopeDeniedError("MCP server executable is not installed")
        with executable.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != self._executable_hash:
            raise StateMismatchError("MCP server executable changed since approval")
        self._launched_definition_hash = definition.fingerprint
        if self._managed_scope is not None:
            await self._open_managed(request)
        else:
            self._process = await self._supervisor.open(request, cancelled=self._cancelled)
        assert self._process is not None
        self._transport = McpStdioTransport(
            self._process.reader,
            self._process.writer,
            request_timeout_s=min(10.0, request.timeout_ms / 1000),
        )
        self._client = McpClient(self._exchange, admit=self._admit)
        try:
            await self._client.connect()
            self._catalog = await self._client.refresh_tools()
            # Refuse grants for tools absent from this exact, pinned discovery.
            for name, grant in self._grants.items():
                discovered_tool = self._catalog.tool(name)
                if (
                    grant.tool_fingerprint is not None
                    and discovered_tool.fingerprint != grant.tool_fingerprint
                ):
                    raise StateMismatchError(
                        "MCP approved tool is not the discovered implementation"
                    )
            return self
        except BaseException:
            await self.close(force=self._syn_route is None)
            raise

    async def _dispatch(self, message: dict[str, Any], expect_reply: bool) -> dict[str, Any] | None:
        process, transport = self._process, self._transport
        if (
            process is None
            or process.closed
            or transport is None
            or process.remaining_seconds() <= 0
            or self._cancel_requested()
        ):
            raise IndeterminateExecutionError("MCP process lifetime ended before dispatch")
        if self._lease is not None:
            await asyncio.to_thread(self._lease.ensure_live)
        transport.timeout = min(10.0, process.remaining_seconds())
        return await transport.exchange(message, expect_reply)

    async def _exchange(self, message: dict[str, Any], expect_reply: bool) -> dict[str, Any] | None:
        if self._syn_route is not None and message.get("method") == "tools/call":
            return await self._syn_route.invoke(message, self._dispatch)
        return await self._dispatch(message, expect_reply)

    async def call(self, name: str, arguments: dict[str, Any]) -> McpCallResult:
        async with self._call_lock:
            return await self._call(name, arguments)

    async def _call(self, name: str, arguments: dict[str, Any]) -> McpCallResult:
        if self._process is None or self._process.closed or self._client is None:
            raise IndeterminateExecutionError("MCP process is not available for invocation")
        if self._catalog is None:
            raise StateMismatchError("MCP tool catalog must be discovered first")
        if self._process.remaining_seconds() <= 0:
            await self.close(force=True)
            raise IndeterminateExecutionError("MCP process lifetime expired")
        tool = self._catalog.tool(name)
        if name not in self._grants:
            raise ScopeDeniedError("MCP tool is not granted to this task")
        try:
            result = await self._client.call_tool(
                name,
                arguments,
                pinned_catalog_hash=self._catalog.fingerprint,
                pinned_tool_hash=tool.fingerprint,
            )
            if self._lease is not None:
                await asyncio.to_thread(self._lease.ensure_live)
            return result
        except BaseException:
            # An error after dispatch can be indeterminate. Never silently
            # restart a server or replay a request, even when labelled a read.
            # Closing stdio fences the session immediately. Give a routed
            # process the supervisor's bounded exit window so the scoped
            # launcher can return its TASKSTATS and shutdown evidence.
            await self.close(force=self._syn_route is None)
            raise

    async def close(self, *, force: bool = False) -> InteractiveExit | None:
        if self._process is None:
            return None
        result = None
        try:
            try:
                if self._transport is not None:
                    await self._transport.close()
            finally:
                result = await self._process.close(force=force)
        finally:
            self._finish_lease(result)
        return result

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @property
    def binding_hash(self) -> str | None:
        """Source/config identity for the caller's durable evidence ledger."""
        if self._launched_definition_hash is None or self._catalog is None:
            return None
        return content_hash(
            {
                "server_definition_hash": self._launched_definition_hash,
                "tool_catalog_hash": self._catalog.fingerprint,
                "isolation_identity": self.isolation_identity,
                "candidate_root": str(self._root),
            }
        )
