"""Live T02.3: intentionally fails when requested without qualified Bubblewrap/seccomp.

Run via scripts/check.py --with-postgres --with-sandbox on Linux CI. These are
NOT satisfied by fake subprocesses used in unit tests.
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from potbot.domain.errors import IndeterminateExecutionError, ScopeDeniedError
from potbot.domain.execution import ActionProposal, CapabilityContext, ToolDefinition, ToolRegistry
from potbot.tools.cli_adapter import ApprovedCliAdapter
from potbot.tools.interactive_supervisor import InteractiveProcessSupervisor
from potbot.tools.mcp_protocol import McpProtocolError
from potbot.tools.mcp_sandbox import ReadOnlyMcpGrant, SandboxedMcpClient
from potbot.tools.supervisor import ProcessSupervisor
from potbot.workspace_sandbox import WorkspaceSandbox

pytestmark = pytest.mark.sandbox


def environment(tmp_path: Path):
    sandbox = WorkspaceSandbox.prepare(tmp_path / "registered-workspace")
    candidate = sandbox.path("candidates") / "stage-t02-3"
    candidate.mkdir(mode=0o700)
    return sandbox, candidate


def digest(binary: str) -> str:
    with open(binary, "rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


@pytest.fixture
def sandboxed_calculator(tmp_path: Path):
    sandbox, candidate = environment(tmp_path)
    fixture_source = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_stdio_server.py"
    fixture_target = candidate / "fixture.py"
    shutil.copy2(fixture_source, fixture_target)
    interpreter_digest = digest(sys.executable)

    def client(*options: str, grants: tuple[str, ...] = ("add",)):
        definition = ToolDefinition(
            name="mcp-offline-fixture",
            capability="tool.read-local",
            argv_prefix=(sys.executable, "-u", str(fixture_target), *options),
            mutates=False,
            accepts_stdin=True,
            timeout_ms=12_000,
            max_output_bytes=4096,
        )
        return SandboxedMcpClient(
            supervisor=InteractiveProcessSupervisor(),
            sandbox=sandbox,
            candidate_root=candidate,
            server_kind=definition.name,
            registry=ToolRegistry((definition,)),
            capabilities=CapabilityContext((definition.name,)),
            task_contract={"capabilities": (definition.capability,)},
            executable_sha256=interpreter_digest,
            permitted=tuple(ReadOnlyMcpGrant(tool) for tool in grants),
            approve_call=lambda *_: None,
        )

    return candidate, client


def test_real_bubblewrap_stdio_mcp(tmp_path: Path, monkeypatch, sandboxed_calculator):
    _, connect = sandboxed_calculator
    canary = tmp_path / "outside-workspace"
    original = b"disposable host canary"
    canary.write_bytes(original)
    monkeypatch.setenv("POTBOT_HOST_TEST_SECRET", "CANARY_NOT_IN_CHILD")

    async def scenario(port: int) -> None:
        async with connect(
            "--scenario", "sandbox_probes", "--host-file", str(canary), "--host-port", str(port)
        ) as client:
            result = await client.call("add", {"a": 30, "b": 12})
            assert result.structured_content == {
                "sum": 42,
                "host_read": False,
                "host_write": False,
                "host_network": False,
                "host_environment": False,
            }
            assert client.isolation_identity and len(client.isolation_identity) == 64

    with socket.create_server(("127.0.0.1", 0)) as listener:
        asyncio.run(scenario(listener.getsockname()[1]))
    assert canary.read_bytes() == original


@pytest.mark.filterwarnings(
    "error::pytest.PytestUnraisableExceptionWarning", "error::ResourceWarning"
)
def test_mcp_bounds_stdio_and_closes_oversized_reply(sandboxed_calculator):
    _, connect = sandboxed_calculator
    arguments = {"a": 30, "b": 12}

    async def scenario() -> None:
        async with connect("--scenario", "noisy_stderr") as client:
            result = await client.call("add", arguments)
            assert result.structured_content == {"sum": 42}
            stderr = await client.close()
            assert stderr is not None and stderr.processes_stopped
            assert stderr.stderr_truncated and stderr.stderr_bytes == 16_385

        async with connect("--scenario", "oversize") as client:
            with pytest.raises(McpProtocolError, match="Oversized MCP stdio line"):
                await client.call("add", arguments)
            with pytest.raises(IndeterminateExecutionError, match="not available"):
                await client.call("add", arguments)
            stopped = await client.close()
            assert stopped is not None and stopped.processes_stopped

    asyncio.run(scenario())
    gc.collect()


def test_mcp_crash_and_restart_does_not_repeat_a_write(sandboxed_calculator):
    candidate, connect = sandboxed_calculator
    journal = candidate / ".mcp-test-effects.jsonl"

    async def scenario() -> None:
        for index in range(100):
            arguments = {"a": index, "b": 1}
            async with connect("--scenario", "effect_crash", grants=("add",)) as client:
                with pytest.raises(IndeterminateExecutionError, match="exited before reply"):
                    await client.call("add", arguments)
                with pytest.raises(IndeterminateExecutionError, match="not available"):
                    await client.call("add", arguments)
                crashed = await client.close()
                assert crashed is not None and crashed.processes_stopped

            # A new lifetime receives a read-only grant and reads the durable effect.
            async with connect("--scenario", "effect_crash", grants=("read_receipt",)) as client:
                with pytest.raises(ScopeDeniedError, match="not granted"):
                    await client.call("add", arguments)
                recovered = await client.call("read_receipt", arguments)
                assert recovered.structured_content == {"sum": index + 1}
                stopped = await client.close()
                assert stopped is not None and stopped.processes_stopped

    asyncio.run(scenario())
    rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert rows == [{"arguments": {"a": index, "b": 1}, "sum": index + 1} for index in range(100)]


def test_qualified_cli_git_python_and_failure(tmp_path: Path):
    sandbox, candidate = environment(tmp_path)
    git = shutil.which("git")
    assert git is not None, "Qualified CI must install Git"
    subprocess.run([git, "init", "--quiet", str(candidate)], check=True, timeout=10)
    commands = (
        ToolDefinition(
            name="git-status",
            capability="tool.read-local",
            argv_prefix=(git, "status", "--porcelain"),
            mutates=False,
        ),
        ToolDefinition(
            name="python-version",
            capability="tool.read-local",
            argv_prefix=(sys.executable, "-V"),
            mutates=False,
        ),
        ToolDefinition(
            name="controlled-failure",
            capability="tool.read-local",
            argv_prefix=(sys.executable, "-c", "raise SystemExit(7)"),
            mutates=False,
        ),
    )
    adapter = ApprovedCliAdapter(
        supervisor=ProcessSupervisor(),
        sandbox=sandbox,
        registry=ToolRegistry(commands),
        candidate_root=candidate,
        capabilities=CapabilityContext(tuple(x.name for x in commands)),
        task_contract={"capabilities": ("tool.read-local",)},
        pinned_executables={git: digest(git), sys.executable: digest(sys.executable)},
    )
    status = adapter.run(ActionProposal(kind="git-status"))
    assert status.outcome == "completed" and status.exit_code == 0
    version = adapter.run(ActionProposal(kind="python-version"))
    assert version.outcome == "completed" and version.exit_code == 0
    assert "Python" in version.stdout + version.stderr
    failed = adapter.run(ActionProposal(kind="controlled-failure"))
    assert failed.outcome == "nonzero_exit" and failed.exit_code == 7
    assert all(x.isolation_identity for x in (status, version, failed))
