"""Live T02.3: intentionally fails when requested without qualified Bubblewrap/seccomp.

Run via scripts/check.py --with-postgres --with-sandbox on Linux CI. These are
NOT satisfied by fake subprocesses used in unit tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from potbot.domain.execution import ActionProposal, CapabilityContext, ToolDefinition, ToolRegistry
from potbot.tools.cli_adapter import ApprovedCliAdapter
from potbot.tools.interactive_supervisor import InteractiveProcessSupervisor
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


def test_real_bubblewrap_stdio_mcp(tmp_path: Path, monkeypatch):
    sandbox, candidate = environment(tmp_path)
    fixture_source = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_stdio_server.py"
    fixture_target = candidate / "fixture.py"
    shutil.copy2(fixture_source, fixture_target)
    # Host secrets and external files must not be mounted into the child.
    monkeypatch.setenv("POTBOT_HOST_TEST_SECRET", "CANARY_NOT_IN_CHILD")
    definition = ToolDefinition(
        name="mcp-offline-fixture",
        capability="tool.read-local",
        argv_prefix=(sys.executable, "-u", str(fixture_target)),
        mutates=False,
        accepts_stdin=True,
        timeout_ms=12_000,
    )

    async def scenario() -> None:
        async with SandboxedMcpClient(
            supervisor=InteractiveProcessSupervisor(),
            sandbox=sandbox,
            candidate_root=candidate,
            server_kind=definition.name,
            registry=ToolRegistry((definition,)),
            capabilities=CapabilityContext((definition.name,)),
            task_contract={"capabilities": (definition.capability,)},
            executable_sha256=digest(sys.executable),
            permitted=(ReadOnlyMcpGrant("add"),),
            approve_call=lambda *_: None,
        ) as client:
            result = await client.call("add", {"a": 30, "b": 12})
            assert result.structured_content == {"sum": 42}
            assert client.isolation_identity and len(client.isolation_identity) == 64

    asyncio.run(scenario())


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
