"""Actual Python debugger through the production Bubblewrap interactive supervisor."""

import asyncio
import hashlib
import shutil
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.debugging import DebugIdentity
from potbot.debugging.acceptance import python_walkthrough
from potbot.debugging.python import DEBUGPY_VERSION
from potbot.debugging.sandbox import SandboxedDebugger
from potbot.tools.interactive_supervisor import InteractiveProcessSupervisor
from potbot.tools.supervisor import ProcessRequest
from potbot.workspace_sandbox import WorkspaceSandbox

pytestmark = pytest.mark.sandbox


def test_real_debugpy_in_qualified_existing_sandbox(tmp_path):
    sandbox = WorkspaceSandbox.prepare(tmp_path / "registered-debugger")
    candidate = sandbox.path("candidates") / "python-fixture"
    candidate.mkdir()
    program = candidate / "nested.py"
    shutil.copyfile(Path(__file__).resolve().parents[2] / "examples/debugging/nested.py", program)
    identity = DebugIdentity(
        uuid4(),
        uuid4(),
        candidate,
        (("nested.py", hashlib.sha256(program.read_bytes()).hexdigest()),),
        "debugpy",
        DEBUGPY_VERSION,
        f"Python {sys.version.split()[0]}",
        "synny://type/workflow-execution",
    )
    request = ProcessRequest(
        argv=(sys.executable, "-I", "-B", "-m", "debugpy.adapter"),
        cwd=candidate,
        workspace_root=candidate,
        sandbox_root=sandbox.root,
        timeout_ms=60_000,
        environment=sandbox.runtime_environment(),
        writable_roots=tuple(sandbox.path(x) for x in ("tmp", "cache")),
    )
    events = []
    host = SandboxedDebugger(
        supervisor=InteractiveProcessSupervisor(),
        request=request,
        identity=identity,
        executable_sha256=hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        admit=lambda *_: None,
        record=events.append,
    )

    async def scenario():
        async with host as session:
            observations = await python_walkthrough(
                session, program=program, python=Path(sys.executable)
            )
            assert observations[-1] == {"action": "natural_exit", "exit_code": 0}
        assert host.exit.processes_stopped
        assert host.exit.isolation_identity and len(host.exit.isolation_identity) == 64

    asyncio.run(scenario())
