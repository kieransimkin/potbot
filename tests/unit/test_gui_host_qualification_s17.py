"""S17 host-evidence policy tests without claiming a real host was qualified."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from potbot.tools.xpra_log_policy_s17 import XpraLogCustody
from scripts import qualify_gui_host_s17 as gate


def test_combined_gate_requires_a_completed_run_and_archived_logs(tmp_path: Path, monkeypatch):
    logs = tmp_path / "logs"
    guest = tmp_path / "guest"
    logs.mkdir(mode=0o700)
    guest.mkdir(mode=0o700)
    owner = XpraLogCustody(logs, guest)
    out = owner.open()
    out.write(b"s17 log")
    owner.close(out)
    ws, run = uuid4(), uuid4()
    monkeypatch.setattr(gate, "verify_gui_run", lambda *a, **k: {
        "tracedProcesses": 4, "fileEvents": 32,
    })
    monkeypatch.setattr(gate, "inspect_recovery", lambda *a, **k: {
        "requiresReconciliation": False, "total": 1,
    })
    report = gate.qualify(tmp_path / "audit", logs, ws, run)
    assert report["qualified"]
    assert report["archivedHostLogs"] == 1
    assert report["fileEvents"] == 32
    assert "guest" not in str(report)


def test_combined_gate_refuses_unsettled_other_runs(tmp_path: Path, monkeypatch):
    ws, run = uuid4(), uuid4()
    monkeypatch.setattr(gate, "verify_gui_run", lambda *a, **k: {
        "tracedProcesses": 1, "fileEvents": 1,
    })
    monkeypatch.setattr(gate, "inspect_recovery", lambda *a, **k: {
        "requiresReconciliation": True, "total": 1,
    })
    monkeypatch.setattr(gate, "inspect_xpra_logs", lambda *a, **k: {
        "requiresReconciliation": False, "instances": [{"archived": 1}],
    })
    with pytest.raises(ValueError):
        gate.qualify(tmp_path / "audit", tmp_path / "logs", ws, run)
