#!/usr/bin/env python3
"""T01.1–T01.4 gate: explicit offline versus fully qualified outcomes.

An offline green result NEVER claims PostgreSQL, sandbox, custody, MCP, provider
or production verification. The qualified gate delegates to the established
Potbot scripts/check.py with mandatory PostgreSQL and sandbox suites.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

try:
    from tool_platform_inventory import build_inventory
except ImportError:
    from scripts.tool_platform_inventory import build_inventory

TEST_MODULES = {
    "T01.1": "tests/unit/test_tool_platform_inventory.py",
    "T01.2": "tests/unit/test_tool_capability_contracts.py",
    "T01.3": "tests/unit/test_tool_invocation_provenance.py",
    "T01.4": "tests/unit/test_tool_phase1_gate.py",
}


def _run(root: Path, args: list[str], *, timeout: int) -> int:
    try:
        result = subprocess.run(
            args, cwd=root, check=False, timeout=timeout,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return result.returncode
    except (OSError, subprocess.TimeoutExpired):
        return 124


def _cases(path: Path) -> dict[str, int]:
    if not path.is_file():
        return {"collected": 0, "failed": 0, "errors": 0, "skipped": 0}
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return {"collected": 0, "failed": 1, "errors": 1, "skipped": 0}
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    return {key: sum(int(s.attrib.get(key_map, 0)) for s in suites)
            for key, key_map in (("collected", "tests"), ("failed", "failures"),
                                 ("errors", "errors"), ("skipped", "skipped"))}


def run_gate(root: Path, artifacts: Path, *, mode: str = "offline") -> dict[str, Any]:
    if mode not in ("offline", "qualified"):
        raise ValueError("Tool platform stage gate mode must be offline or qualified")
    root = root.resolve(strict=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    inventory = build_inventory(root)
    (artifacts / "source-inventory.json").write_text(
        json.dumps(inventory, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    stages: list[dict[str, Any]] = []
    for stage_id, selector in TEST_MODULES.items():
        junit = artifacts / f"{stage_id.lower().replace('.', '-')}.xml"
        exit_code = _run(root, [sys.executable, "-m", "pytest", "-q", selector, f"--junitxml={junit}"], timeout=180)
        summary = _cases(junit)
        stages.append({
            "stage": stage_id,
            "test_selector": selector,
            "status": "offline_passed" if exit_code == 0 and summary["collected"] > 0
                       and summary["failed"] == 0 and summary["errors"] == 0
                       and summary["skipped"] == 0 else "failed",
            "return_code": exit_code,
            "tests": summary,
        })
    if inventory["missing_required_anchors"]:
        stages[0]["status"] = "failed"
    offline_passed = all(row["status"] == "offline_passed" for row in stages)
    qualified = False
    qualification: dict[str, Any] = {
        "requested": mode == "qualified", "performed": False,
        "postgresql": "not_run", "sandbox": "not_run", "check_success": False,
    }
    if mode == "qualified" and offline_passed:
        # Tests + database migrations + sandbox + wheel checks executed by the
        # repository's existing trusted runner. No shadow DB runner is added.
        report_dir = artifacts / "full-check"
        exit_code = _run(root, [
            sys.executable, str(root / "scripts" / "check.py"),
            "--with-postgres", "--with-sandbox", "--artifacts", str(report_dir),
        ], timeout=1800)
        state_path = report_dir / "summary.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        qualified = bool(
            exit_code == 0 and state.get("success") is True
            and state.get("postgres_requested") is True
            and state.get("sandbox_requested") is True
            and any(x.get("check") == "postgres" and x.get("exit_code") == 0 for x in state.get("checks", []))
            and any(x.get("check") == "sandbox" and x.get("exit_code") == 0 for x in state.get("checks", []))
        )
        qualification = {
            "requested": True, "performed": state_path.is_file(),
            "postgresql": "passed" if qualified else "failed_or_unavailable",
            "sandbox": "passed" if qualified else "failed_or_unavailable",
            "check_success": qualified,
            "return_code": exit_code,
        }
    report: dict[str, Any] = {
        "schema_version": 1,
        "programme": "potbot-universal-tool-mcp-integration",
        "phase": "T01",
        "source_inventory_hash": inventory["inventory_sha256"],
        "source_head": inventory["checkout_head"],
        "mode": mode,
        "offline_contract_checks_passed": offline_passed,
        "fully_qualified": qualified,
        "qualification": qualification,
        "stages": stages,
        "notes": [
            "This gate tests interface contracts and source anchors only, not tool execution",
            "No MCP server, browser, external network, paid model or credential is invoked",
            "A successful offline report does not qualify PostgreSQL or the Linux sandbox",
            "Only an explicitly requested successful full-check gate can qualify T01",
        ],
    }
    digest = hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    report["report_sha256"] = digest
    (artifacts / "phase1-gate.json").write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--mode", choices=("offline", "qualified"), default="offline")
    args = parser.parse_args()
    artifacts = args.artifacts or args.root / "artifacts" / "tool-platform-phase1"
    result = run_gate(args.root, artifacts, mode=args.mode)
    print(json.dumps({
        "phase": result["phase"], "mode": result["mode"],
        "offline_contract_checks_passed": result["offline_contract_checks_passed"],
        "fully_qualified": result["fully_qualified"],
        "report": str(artifacts / "phase1-gate.json"),
    }, sort_keys=True))
    if result["mode"] == "qualified":
        return 0 if result["fully_qualified"] else 1
    return 0 if result["offline_contract_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
