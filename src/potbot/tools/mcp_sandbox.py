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

import hashlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.execution import ActionProposal, CapabilityContext, ToolRegistry
from potbot.domain.serialization import content_hash
from potbot.tools.supervisor import ProcessRequest
from potbot.workspace_sandbox import WorkspaceSandbox

from potbot.tools.interactive_supervisor import (
    InteractiveExit,
    InteractiveProcessSupervisor,
    IsolatedInteractiveProcess,
)
from potbot.tools.mcp_protocol import McpCallResult, McpCatalog, McpClient, McpToolInfo
from potbot.tools.mcp_stdio import McpStdioTransport

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
    ) -> None:
        if not isinstance(supervisor, InteractiveProcessSupervisor):
            raise InvalidInputError("MCP requires the qualified interactive ProcessSupervisor")
        if not isinstance(sandbox, WorkspaceSandbox):
            raise InvalidInputError("MCP requires a registered WorkspaceSandbox")
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

    async def __aenter__(self) -> SandboxedMcpClient:
        if self._process is not None:
            raise StateMismatchError("An MCP server lifetime cannot be restarted")
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
        executable = Path(definition.argv_prefix[0])
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ScopeDeniedError("MCP server executable is not installed")
        with executable.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != self._executable_hash:
            raise StateMismatchError("MCP server executable changed since approval")
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
        self._launched_definition_hash = definition.fingerprint
        self._process = await self._supervisor.open(request, cancelled=self._cancelled)
        self._transport = McpStdioTransport(
            self._process.reader,
            self._process.writer,
            request_timeout_s=min(10.0, request.timeout_ms / 1000),
        )
        self._client = McpClient(self._transport.exchange, admit=self._admit)
        try:
            await self._client.connect()
            self._catalog = await self._client.refresh_tools()
            # Refuse grants for tools absent from this exact, pinned discovery.
            for name, grant in self._grants.items():
                definition = self._catalog.tool(name)
                if (
                    grant.tool_fingerprint is not None
                    and definition.fingerprint != grant.tool_fingerprint
                ):
                    raise StateMismatchError(
                        "MCP approved tool is not the discovered implementation"
                    )
            return self
        except BaseException:
            await self.close(force=True)
            raise

    async def call(self, name: str, arguments: dict[str, Any]) -> McpCallResult:
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
        assert self._transport is not None
        self._transport.timeout = min(10.0, self._process.remaining_seconds())
        try:
            return await self._client.call_tool(
                name,
                arguments,
                pinned_catalog_hash=self._catalog.fingerprint,
                pinned_tool_hash=tool.fingerprint,
            )
        except BaseException:
            # An error after dispatch can be indeterminate. Never silently
            # restart a server or replay a request, even when labelled a read.
            await self.close(force=True)
            raise

    async def close(self, *, force: bool = False) -> InteractiveExit | None:
        if self._process is None:
            return None
        if self._transport is not None:
            try:
                await self._transport.close()
            except (OSError, ConnectionError):
                pass
        return await self._process.close(force=force)

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
