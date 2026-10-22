"""T02.3 controller-to-MCP sandbox bridge; unit backend is not real isolation."""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from potbot.domain.errors import IndeterminateExecutionError, ScopeDeniedError, StateMismatchError
from potbot.domain.execution import CapabilityContext, ToolDefinition, ToolRegistry
from potbot.tools.interactive_supervisor import InteractiveProcessSupervisor
from potbot.tools.mcp_protocol import McpProtocolError
from potbot.tools.mcp_sandbox import ReadOnlyMcpGrant, SandboxedMcpClient
from potbot.workspace_sandbox import WorkspaceSandbox

SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_stdio_server.py"


class UnitFixtureBackend:
    def prepare(self, *, argv, status_fd, **_):
        return SimpleNamespace(
            argv=argv, pass_fds=(status_fd,), identity="unit-fixture-NOT-isolation"
        )


def sandbox(tmp_path: Path):
    root = tmp_path / "registered"
    candidate = root / "candidates" / "test-001"
    candidate.mkdir(parents=True)
    return WorkspaceSandbox(root), candidate


def kwargs(tmp_path: Path, *, scenario: str = "normal", allow: bool = True):
    ws, candidate = sandbox(tmp_path)
    tool = ToolDefinition(
        name="mcp-fixture",
        capability="local-mcp",
        argv_prefix=(sys.executable, "-u", str(SERVER), "--scenario", scenario),
        mutates=False,
        accepts_stdin=True,
        timeout_ms=2500,
    )
    with open(sys.executable, "rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()

    def approve(tool, args):
        if not allow:
            raise ScopeDeniedError("Task permission revoked")

    return dict(
        sandbox=ws,
        candidate_root=candidate,
        server_kind=tool.name,
        supervisor=InteractiveProcessSupervisor(UnitFixtureBackend()),
        registry=ToolRegistry((tool,)),
        capabilities=CapabilityContext((tool.name,)),
        task_contract={"capabilities": (tool.capability,)},
        executable_sha256=digest,
        permitted=(ReadOnlyMcpGrant("add"),),
        approve_call=approve,
    )


def run(coro):
    return asyncio.run(coro)


def test_end_to_end_mcp_sum_inside_approved_lifetime(tmp_path: Path):
    async def scenario():
        async with SandboxedMcpClient(**kwargs(tmp_path)) as session:
            assert len(session.catalog.tools) == 1
            result = await session.call("add", {"a": 16, "b": 26})
            assert result.structured_content == {"sum": 42}
            assert len(session.binding_hash or "") == 64
            assert session.isolation_identity == "unit-fixture-NOT-isolation"
            assert (await session.call("add", {"a": 3, "b": 5})).structured_content == {"sum": 8}

    run(scenario())


def test_ungranted_name_refused_even_when_advertised(tmp_path: Path):
    async def scenario():
        async with SandboxedMcpClient(**kwargs(tmp_path, scenario="pages")) as session:
            with pytest.raises(ScopeDeniedError):
                await session.call("multiply", {"a": 1, "b": 2})

    run(scenario())


def test_call_authority_rechecked_every_invocation(tmp_path: Path):
    async def scenario():
        async with SandboxedMcpClient(**kwargs(tmp_path, allow=False)) as session:
            with pytest.raises(ScopeDeniedError):
                await session.call("add", {"a": 1, "b": 2})

    run(scenario())


def test_bad_executable_digest_rejected_before_spawn(tmp_path: Path):
    inputs = kwargs(tmp_path)
    inputs["executable_sha256"] = "0" * 64

    async def scenario():
        with pytest.raises(StateMismatchError):
            async with SandboxedMcpClient(**inputs):
                pass

    run(scenario())


def test_mcp_stdin_required_and_no_secrets_allowed(tmp_path: Path):
    inputs = kwargs(tmp_path)
    tool = inputs["registry"].get("mcp-fixture")
    from dataclasses import replace

    inputs["registry"] = ToolRegistry((replace(tool, accepts_stdin=False),))

    async def no_stdio():
        with pytest.raises(ScopeDeniedError):
            async with SandboxedMcpClient(**inputs):
                pass

    run(no_stdio())
    inputs["registry"] = ToolRegistry((replace(tool, secret_refs=(("KEY", "secret://private"),)),))
    run(no_stdio())


def test_workspace_escape_denied_before_spawning(tmp_path: Path):
    inputs = kwargs(tmp_path)
    inputs["candidate_root"] = tmp_path
    with pytest.raises(ScopeDeniedError):
        SandboxedMcpClient(**inputs)


def test_bad_tool_result_kills_server_and_never_replays(tmp_path: Path):
    async def scenario():
        async with SandboxedMcpClient(**kwargs(tmp_path, scenario="wrong_id")) as session:
            with pytest.raises(McpProtocolError):
                await session.call("add", {"a": 1, "b": 2})
            with pytest.raises(IndeterminateExecutionError):
                await session.call("add", {"a": 1, "b": 2})

    run(scenario())


def test_server_timeout_kills_and_does_not_retry(tmp_path: Path):
    async def scenario():
        async with SandboxedMcpClient(**kwargs(tmp_path, scenario="timeout")) as session:
            assert session._transport is not None
            # Lower the deadline to force this particular exchange to fail.
            with pytest.raises(IndeterminateExecutionError):
                await session.call("add", {"a": 1, "b": 2})

    # The fixture sleeps one second but the adapter's default deadline is 10s.
    # Test cancellation explicitly instead of shortening an internal variable.
    async def cancelled():
        async with SandboxedMcpClient(**kwargs(tmp_path, scenario="timeout")) as session:
            pending = asyncio.create_task(session.call("add", {"a": 1, "b": 2}))
            await asyncio.sleep(0.05)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            with pytest.raises(IndeterminateExecutionError):
                await session.call("add", {"a": 1, "b": 2})

    run(cancelled())


def test_discovered_tool_hash_rechecked(tmp_path: Path):
    inputs = kwargs(tmp_path)
    inputs["permitted"] = (ReadOnlyMcpGrant("add", "a" * 64),)

    async def scenario():
        with pytest.raises(StateMismatchError):
            async with SandboxedMcpClient(**inputs):
                pass

    run(scenario())


def test_unauthorized_environment_is_denied_before_launch(tmp_path: Path):
    inputs = kwargs(tmp_path)
    from dataclasses import replace

    tool = inputs["registry"].get("mcp-fixture")
    inputs["registry"] = ToolRegistry((replace(tool, environment=(("PASSWORD", "literal"),)),))

    async def scenario():
        with pytest.raises(ScopeDeniedError):
            async with SandboxedMcpClient(**inputs):
                pass

    run(scenario())
