"""Release-gate regressions: never convert missing/invalid evidence into success."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "qualify_operator_workers.py"
_spec = importlib.util.spec_from_file_location("operator_worker_qualification", _SCRIPT)
assert _spec and _spec.loader
_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gate)


def test_zero_exit_without_attestation_is_rejected(monkeypatch):
    monkeypatch.setattr(
        _gate.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="garbage", stderr=""),
    )
    assert _gate.run_command(["ignored"], timeout=5)["passed"] is False


def test_empty_output_is_rejected(monkeypatch):
    monkeypatch.setattr(
        _gate.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr="")
    )
    assert _gate.run_command(["ignored"], timeout=5)["passed"] is False


def test_multiple_reports_are_rejected(monkeypatch):
    monkeypatch.setattr(
        _gate.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout='{}\n{"qualified":true}', stderr=""),
    )
    assert _gate.run_command(["ignored"], timeout=5)["passed"] is False


def test_explicit_success_field_and_status_required(monkeypatch):
    result = SimpleNamespace(returncode=0, stdout=json.dumps({"qualified_smoke": True}), stderr="")
    monkeypatch.setattr(_gate.subprocess, "run", lambda *a, **k: result)
    assert (
        _gate.run_command(["ignored"], timeout=5, success_field="qualified_smoke")["passed"] is True
    )
    assert _gate.run_command(["ignored"], timeout=5)["passed"] is False
    result.returncode = 1
    assert (
        _gate.run_command(["ignored"], timeout=5, success_field="qualified_smoke")["passed"]
        is False
    )


def test_passes_disposable_ai_mounts_to_live_worker_probe(monkeypatch, tmp_path):
    calls = []

    def record_command(argv, **_):
        calls.append(argv)
        return {"passed": True}

    fields = {
        "rootfs": tmp_path / "rootfs",
        "home": tmp_path / "home",
        "model-staging": tmp_path / "staging",
        "shared-models": tmp_path / "models",
        "shared-tools": tmp_path / "tools",
        "image-digest": "1" * 64,
        "qualification-sha256": "2" * 64,
        "report": tmp_path / "worker.json",
        "ai-config": tmp_path / "ai",
        "ai-past": tmp_path / "history",
    }
    monkeypatch.setattr(
        sys,
        "argv",
        ["probe", *(item for name, value in fields.items() for item in (f"--{name}", str(value)))],
    )
    monkeypatch.setattr(_gate, "inspect_host", lambda: {"passed": True})
    monkeypatch.setattr(_gate, "run_command", record_command)
    monkeypatch.setattr(_gate.os, "O_NOFOLLOW", getattr(_gate.os, "O_NOFOLLOW", 0), raising=False)
    assert _gate.main() == 0
    assert calls[-1][-4:] == [
        "--ai-config",
        str(fields["ai-config"]),
        "--ai-past",
        str(fields["ai-past"]),
    ]


@pytest.mark.parametrize("collector_ready", [True, False])
def test_scoped_qualification_uses_the_broker_at_both_gates(monkeypatch, tmp_path, collector_ready):
    calls = []

    def record_command(argv, **_):
        calls.append(argv)
        return {"passed": collector_ready}

    options = {
        "rootfs": tmp_path / "rootfs",
        "home": tmp_path / "home",
        "model-staging": tmp_path / "staging",
        "shared-models": tmp_path / "models",
        "shared-tools": tmp_path / "tools",
        "image-digest": "1" * 64,
        "qualification-sha256": "2" * 64,
        "report": tmp_path / "worker.json",
    }
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "probe",
            "--scoped-worker-launcher",
            *(item for name, value in options.items() for item in (f"--{name}", str(value))),
        ],
    )
    monkeypatch.setattr(_gate, "inspect_host", lambda: {"passed": True})
    monkeypatch.setattr(_gate, "run_command", record_command)
    monkeypatch.setattr(_gate.os, "O_NOFOLLOW", getattr(_gate.os, "O_NOFOLLOW", 0), raising=False)
    assert _gate.main() == (0 if collector_ready else 1)
    assert len(calls) == (2 if collector_ready else 1)
    assert all("--scoped-worker-launcher" in call for call in calls)
    assert calls[0][1] == "scripts/qualify_worker_exit_accounting.py"
    if collector_ready:
        assert calls[1][1] == "scripts/qualify_phase35_worker.py"
    summary = json.loads((tmp_path / "worker.operator-summary.json").read_text())
    assert summary["collector"] == "scoped-launcher"
    assert summary["qualified"] is collector_ready
