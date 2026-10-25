"""The sandbox smoke cannot pass when denial commands were never dispatched."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


def _qualifier():
    source = Path(__file__).resolve().parents[2] / "scripts/qualify_shared_workspace.py"
    spec = importlib.util.spec_from_file_location("potbot_shared_qualification", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _result(*, outcome: str, exit_code: int | None, dispatched: bool = True,
            stopped: bool = True, identity: str | None = "isolation-pin") -> SimpleNamespace:
    return SimpleNamespace(
        outcome=outcome, exit_code=exit_code, stdout=b"", stderr=b"",
        dispatched=dispatched, processes_stopped=stopped, isolation_identity=identity,
    )


@pytest.mark.parametrize(
    "case,must_succeed,expected",
    [
        (_result(outcome="completed", exit_code=0), True, True),
        (_result(outcome="nonzero_exit", exit_code=1), False, True),
        (_result(outcome="nonzero_exit", exit_code=1, dispatched=False), False, False),
        (_result(outcome="nonzero_exit", exit_code=1, stopped=False), False, False),
        (_result(outcome="nonzero_exit", exit_code=1, identity=None), False, False),
        (_result(outcome="infrastructure_error", exit_code=None), False, False),
        (_result(outcome="timeout", exit_code=None), False, False),
        (_result(outcome="completed", exit_code=0), False, False),
        (_result(outcome="nonzero_exit", exit_code=5), True, False),
        (_result(outcome="completed", exit_code=0, dispatched=False), True, False),
    ],
)
def test_negative_case_is_never_confused_with_failed_sandbox_launch(
    tmp_path: Path, case: SimpleNamespace, must_succeed: bool, expected: bool,
) -> None:
    class FakeSupervisor:
        def run(self, request: object) -> SimpleNamespace:
            return case

    result = _qualifier()._case(FakeSupervisor(), home=tmp_path,
                                command="false", must_succeed=must_succeed)
    assert result["passed"] is expected
    assert result["dispatched"] is case.dispatched
    assert result["processes_stopped"] is case.processes_stopped


@pytest.mark.skipif(os.name != "posix", reason="Linux/WSL /proc namespace identifiers")
def test_smoke_checks_actual_kernel_network_namespace(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".tmp").mkdir(parents=True)
    staging = tmp_path / "staging"
    tools = tmp_path / "tools"
    staging.mkdir()
    tools.mkdir()
    commands: list[str] = []

    class FakeSupervisor:
        def run(self, request: object) -> SimpleNamespace:
            command = request.argv[-1]
            commands.append(command)
            negative = command.startswith("touch /opt/potbot/models/") or (
                command.startswith("touch /etc/")
                or command.startswith("echo forbidden > /dev/tcp/")
            )
            return _result(outcome="nonzero_exit" if negative else "completed",
                           exit_code=1 if negative else 0)

    runtime = SimpleNamespace(home=home, model_staging=staging,
                              shared_tools=tools, image_digest="a" * 64)
    # Supply the fake supervisor by replacing the constructor, retaining the
    # production smoke test's actual case list and receipt logic.
    module = _qualifier()
    from unittest.mock import patch

    with patch.object(module, "ProcessSupervisor", return_value=FakeSupervisor()), \
         patch.object(module, "SharedWorkspaceBubblewrapBackend", return_value=object()):
        report = module.qualify(runtime)
    assert report["qualified_smoke"] is True
    assert report["tests"]["network_namespace_isolated"]["passed"] is True
    namespace_check = next(command for command in commands
                           if "/proc/self/ns/net" in command)
    assert os.readlink("/proc/self/ns/net") in namespace_check
    assert len(commands) == 11
