from __future__ import annotations

import sys
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.tools.isolation import BubblewrapBackend
from potbot.tools.supervisor import ProcessRequest, ProcessSupervisor


@pytest.mark.parametrize(
    "change",
    [
        {"timeout_ms": True},
        {"max_output_bytes": 0},
        {"argv": ("python",)},
        {"environment": {"LD_PRELOAD": "/bad/library"}},
        {"environment": {"BAD": "bad\x00value"}},
    ],
)
def test_invalid_process_contracts_rejected(tmp_path: Path, change: dict[str, object]) -> None:
    arguments = {
        "argv": (sys.executable, "-c", "pass"),
        "cwd": tmp_path,
        "workspace_root": tmp_path,
        **change,
    }
    with pytest.raises(InvalidInputError):
        ProcessRequest(**arguments)


def test_unavailable_isolation_never_launches_host_command(tmp_path: Path) -> None:
    sentinel = tmp_path / "must-not-exist"
    supervisor = ProcessSupervisor(BubblewrapBackend(executable=tmp_path / "missing-backend"))
    result = supervisor.run(
        ProcessRequest(
            (sys.executable, "-c", f"open({str(sentinel)!r},'w').write('unsafe')"),
            tmp_path,
            tmp_path,
        )
    )
    assert result.outcome == "infrastructure_error"
    assert result.error_code == "capability_unavailable"
    assert not result.dispatched and not sentinel.exists()


def test_missing_executable_is_operational_failure(tmp_path: Path) -> None:
    result = ProcessSupervisor().run(
        ProcessRequest((str(tmp_path / "missing"),), tmp_path, tmp_path)
    )
    assert result.outcome == "infrastructure_error"
    assert result.error_code == "source_unavailable"
    assert not result.dispatched
