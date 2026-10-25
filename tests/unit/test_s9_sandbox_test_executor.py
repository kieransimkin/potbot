"""Contract-only supervisor tests. The Linux/WSL2 host gate is separate."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.domain.errors import ScopeDeniedError
from potbot.tools.shared_tool_upgrades import (
    SandboxedToolTestExecutor,
    ToolTestCase,
    ToolTestSuite,
)
from potbot.tools.shared_workspace_isolation import SharedWorkspaceRuntime
from potbot.tools.supervisor import ProcessSupervisor


class ReceiptSupervisor(ProcessSupervisor):
    def __init__(self, backend):
        super().__init__(backend)
        self.request = None
        self.should_fail = False

    def run(self, request, *, cancelled):
        assert not cancelled()
        self.request = request
        return SimpleNamespace(
            stdout=b"smoke", stderr=b"", exit_code=1 if self.should_fail else 0,
            outcome="nonzero_exit" if self.should_fail else "completed",
            dispatched=True, processes_stopped=True, isolation_identity="qualified-fixture",
            elapsed_ms=12,
        )


def test_supervised_tool_case_binds_agent_and_resource_limits(tmp_path: Path):
    rootfs = tmp_path / "root"
    home = tmp_path / "home"
    rootfs.mkdir(); (home / "alice").mkdir(parents=True)
    runtime = SharedWorkspaceRuntime("a" * 64, rootfs, home, "b" * 64)
    record = []

    def factory(backend):
        supervisor = ReceiptSupervisor(backend)
        record.append(supervisor)
        return supervisor

    runner = SandboxedToolTestExecutor(runtime, supervisor_factory=factory)
    case = ToolTestCase("startup", "smoke", ("/bin/bash", "-c", "true"), 10_000)
    ws, actor = uuid4(), uuid4()
    data = runner.run(case=case, argv=case.argv, workspace_id=ws, agent_id=actor,
                      agent_dir=home / "alice", uid=420, gid=69, cancelled=lambda: False)
    assert data["passed"] is True
    assert data["dispatched"] is True and data["stopped"] is True
    request = record[0].request
    assert request.guest_uid == 420 and request.guest_gid == 69
    assert request.telemetry_workspace_id == ws and request.telemetry_agent_id == actor
    assert request.timeout_ms == 10_000 and request.memory_bytes == 8 * 1024**3
    assert request.sandbox_root == home


def test_supervised_test_nonzero_is_not_qualified(tmp_path: Path):
    rootfs, home = tmp_path / "image", tmp_path / "home"
    rootfs.mkdir(); home.mkdir()
    supervisor = None

    def factory(backend):
        nonlocal supervisor
        supervisor = ReceiptSupervisor(backend)
        supervisor.should_fail = True
        return supervisor

    runner = SandboxedToolTestExecutor(
        SharedWorkspaceRuntime("a" * 64, rootfs, home, "b" * 64),
        supervisor_factory=factory,
    )
    case = ToolTestCase("regression", "unit", ("/bin/bash", "-c", "exit 1"))
    value = runner.run(case=case, argv=case.argv, workspace_id=uuid4(),
                       agent_id=uuid4(), agent_dir=home, uid=420, gid=69,
                       cancelled=lambda: False)
    assert value["passed"] is False
    assert value["exit_code"] == 1


def test_supervisor_factory_must_be_qualified(tmp_path: Path):
    rootfs, home = tmp_path / "image", tmp_path / "home"
    rootfs.mkdir(); home.mkdir()
    runner = SandboxedToolTestExecutor(
        SharedWorkspaceRuntime("a" * 64, rootfs, home, "b" * 64),
        supervisor_factory=lambda _: object(),
    )
    case = ToolTestCase("unit", "unit", ("/bin/bash", "-c", "true"))
    with pytest.raises(ScopeDeniedError):
        runner.run(case=case, argv=case.argv, workspace_id=uuid4(),
                   agent_id=uuid4(), agent_dir=home, uid=420, gid=69,
                   cancelled=lambda: False)


def test_case_templates_and_suite_require_exact_names():
    with pytest.raises(ValueError):
        ToolTestCase("bad", "unit", ("/bin/bash", "--", "{host_secret}"))
    with pytest.raises(ValueError):
        ToolTestCase("Bad space", "unit", ("/bin/bash",))
    with pytest.raises(ValueError):
        ToolTestCase("bad", "unit", ("/usr/local/tools/agent/bin/python",))
    with pytest.raises(ValueError):
        ToolTestSuite((
            ToolTestCase("smoke", "smoke", ("/bin/bash",)),
            ToolTestCase("smoke", "unit", ("/bin/bash",)),
        ))
