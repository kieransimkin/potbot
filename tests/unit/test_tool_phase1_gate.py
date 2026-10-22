"""T01.4 — offline stage passing never masquerades as a qualified release."""

from __future__ import annotations

import json
from pathlib import Path

from scripts import tool_phase1_gate as gate
from scripts.tool_platform_inventory import ANCHORS


def seed(tmp_path: Path) -> None:
    for row in ANCHORS:
        if row.decision == "missing":
            continue
        target = tmp_path / row.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture", encoding="utf-8")


def mock_check_success(tmp_path: Path, args: list[str], *, timeout: int) -> int:
    for arg in args:
        if arg.startswith("--junitxml="):
            report = Path(arg.partition("=")[2])
            report.write_text('<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0"/></testsuites>')
            return 0
    if "--with-postgres" in args and "--with-sandbox" in args:
        artifacts = Path(args[args.index("--artifacts") + 1])
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "summary.json").write_text(json.dumps({
            "success": True, "postgres_requested": True, "sandbox_requested": True,
            "checks": [
                {"check": "postgres", "exit_code": 0},
                {"check": "sandbox", "exit_code": 0},
            ],
        }))
    return 0


def test_offline_stage_report_explicitly_unqualified(tmp_path: Path, monkeypatch) -> None:
    seed(tmp_path)
    monkeypatch.setattr(gate, "_run", mock_check_success)
    report = gate.run_gate(tmp_path, tmp_path / "stage-artifacts", mode="offline")
    assert report["offline_contract_checks_passed"] is True
    assert report["fully_qualified"] is False
    assert all(stage["status"] == "offline_passed" for stage in report["stages"])
    assert (tmp_path / "stage-artifacts" / "source-inventory.json").is_file()
    assert (tmp_path / "stage-artifacts" / "phase1-gate.json").is_file()


def test_qualified_release_requires_existing_db_and_sandbox_gates(tmp_path: Path, monkeypatch) -> None:
    seed(tmp_path)
    monkeypatch.setattr(gate, "_run", mock_check_success)
    report = gate.run_gate(tmp_path, tmp_path / "qualified", mode="qualified")
    assert report["fully_qualified"] is True
    assert report["qualification"]["performed"] is True


def test_failed_existing_check_never_qualifies(tmp_path: Path, monkeypatch) -> None:
    seed(tmp_path)

    def fail_full(root: Path, args: list[str], *, timeout: int) -> int:
        if "--with-postgres" in args:
            return 124
        return mock_check_success(root, args, timeout=timeout)

    monkeypatch.setattr(gate, "_run", fail_full)
    report = gate.run_gate(tmp_path, tmp_path / "broken", mode="qualified")
    assert report["offline_contract_checks_passed"] is True
    assert report["fully_qualified"] is False


def test_skipped_stage_check_is_a_failure(tmp_path: Path, monkeypatch) -> None:
    seed(tmp_path)

    def skipped(root: Path, args: list[str], *, timeout: int) -> int:
        for arg in args:
            if arg.startswith("--junitxml="):
                Path(arg.partition("=")[2]).write_text('<testsuite tests="2" failures="0" errors="0" skipped="1"/>')
                return 0
        return 0

    monkeypatch.setattr(gate, "_run", skipped)
    report = gate.run_gate(tmp_path, tmp_path / "skipped", mode="offline")
    assert report["offline_contract_checks_passed"] is False
