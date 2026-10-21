"""Failed or interrupted verification must never publish a successful summary."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def check_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "check.py"
    spec = importlib.util.spec_from_file_location("phase_one_check_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setattr(module, "ROOT", repository)
    return module


def test_unexpected_runner_error_is_a_sanitized_failure(
    check_script: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts = tmp_path / "artifacts"
    secret = "runner-secret-that-must-not-leak"
    monkeypatch.setenv("CI_TOKEN", secret)
    monkeypatch.setattr(sys, "argv", ["check.py", "--artifacts", str(artifacts)])

    def unavailable_process(*args: object, **kwargs: object) -> None:
        raise RuntimeError(f"unexpected process failure: {secret}")

    monkeypatch.setattr(check_script.subprocess, "run", unavailable_process)
    assert check_script.main() == 1
    assert json.loads((artifacts / "summary.json").read_text())["success"] is False
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert "Unexpected check runner error (RuntimeError)" in captured.err


def test_interrupted_cleanup_preserves_private_recovery_manifest(
    check_script: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    capability = "c" * 64
    original_manifest = json.dumps({"ownership_token": capability})
    monkeypatch.setattr(sys, "argv", ["check.py", "--with-postgres", "--artifacts", str(artifacts)])

    def process(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["uv", "build"]:
            distribution = Path(command[command.index("--out-dir") + 1])
            distribution.mkdir()
            (distribution / "test-wheel.whl").touch()
        if "db" in command and "create" in command:
            Path(command[command.index("--manifest") + 1]).write_text(original_manifest)
        if "db" in command and "cleanup" in command:
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(check_script.subprocess, "run", process)
    assert check_script.main() == 130
    summary = json.loads((artifacts / "summary.json").read_text())
    assert summary["success"] is False
    assert summary["postgres_requested"] is True
    recovery = list((check_script.ROOT / ".test-runs").glob("recovery-*.json"))
    assert len(recovery) == 1
    assert recovery[0].read_text() == original_manifest
    if os.name != "nt":
        assert stat.S_IMODE(recovery[0].stat().st_mode) == 0o600
    assert all(capability not in path.read_text() for path in artifacts.iterdir())
