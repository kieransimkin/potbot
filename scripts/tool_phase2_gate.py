#!/usr/bin/env python3
"""Honest, reproducible T02.1/T02.2 transport gate.

An offline pass proves fixture protocol behavior only. It never qualifies
interactive OS sandbox launches, DNS/IP egress enforcement, OAuth consent or
replay-safe production execution. Existing scripts/check.py remains the sole
full application/DB/sandbox CI runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TESTS = {
    "T02.1": ["tests/unit/test_mcp_protocol.py", "tests/unit/test_mcp_stdio.py"],
    "T02.2": ["tests/unit/test_mcp_http.py"],
}
SOURCES = [
    "src/potbot/tools/mcp_protocol.py",
    "src/potbot/tools/mcp_stdio.py",
    "src/potbot/tools/mcp_http.py",
    "src/potbot/tools/capability_contracts.py",
    "tests/fixtures/mcp_stdio_server.py",
]


def cases(path: Path) -> dict[str, int]:
    if not path.is_file():
        return {"tests": 0, "failures": 0, "errors": 1, "skipped": 0}
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return {"tests": 0, "failures": 0, "errors": 1, "skipped": 0}
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    return {
        key: sum(int(s.attrib.get(key, 0)) for s in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }


def run(root: Path, artifacts: Path, *, mode: str = "offline") -> dict[str, Any]:
    if mode not in ("offline", "qualified"):
        raise ValueError("Unknown tool phase 2 gate mode")
    root = root.resolve(strict=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    source_hashes: dict[str, str | None] = {}
    for relative in SOURCES:
        item = root / relative
        source_hashes[relative] = (
            hashlib.sha256(item.read_bytes()).hexdigest()
            if item.is_file() and not item.is_symlink()
            else None
        )
    tests: list[dict[str, Any]] = []
    for stage_id, selectors in TESTS.items():
        report = artifacts / f"{stage_id.replace('.', '-').lower()}.xml"
        try:
            process = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", *selectors, f"--junitxml={report}"],
                cwd=root,
                check=False,
                capture_output=True,
                timeout=240,
                text=True,
            )
            exit_code = process.returncode
            # Do not persist arbitrary subprocess logs; they might contain test credentials.
        except (OSError, subprocess.TimeoutExpired):
            exit_code = 124
        totals = cases(report)
        tests.append(
            {
                "stage": stage_id,
                "exit_code": exit_code,
                "test_counts": totals,
                "status": "offline_passed"
                if exit_code == 0
                and totals["tests"] > 0
                and totals["failures"] == totals["errors"] == totals["skipped"] == 0
                else "failed",
            }
        )
    success = all(x is not None for x in source_hashes.values()) and all(
        x["status"] == "offline_passed" for x in tests
    )
    qualified_application_checks = "not_run"
    if mode == "qualified" and success:
        full_dir = artifacts / "full-repository"
        full_dir.mkdir(exist_ok=True)
        try:
            process = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts/check.py"),
                    "--with-postgres",
                    "--with-sandbox",
                    "--artifacts",
                    str(full_dir),
                ],
                cwd=root,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1800,
            )
            summary = full_dir / "summary.json"
            details = json.loads(summary.read_text(encoding="utf-8")) if summary.is_file() else {}
            qualified_application_checks = (
                "passed"
                if process.returncode == 0
                and details.get("success") is True
                and details.get("postgres_requested") is True
                and details.get("sandbox_requested") is True
                else "failed"
            )
        except (OSError, subprocess.TimeoutExpired):
            qualified_application_checks = "failed"
    output = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "offline_transport_tests_passed": success,
        "application_full_checks": qualified_application_checks,
        "phase2_fully_qualified": False,
        "outstanding_gates": [
            "Qualified interactive MCP child launch through workspace sandbox ProcessSupervisor (T02.3)",
            "Network egress DNS/IP pinning and OAuth resource/audience-scoped credential broker (T02.2/T03.2/T03.3)",
            "Durable controller admission and no-duplicate-side-effect receipt/recovery path (T03.4)",
            "Live third-party modern and legacy MCP server conformance tests",
        ],
        "files_sha256": source_hashes,
        "stages": tests,
    }
    (artifacts / "phase2-gate.json").write_text(
        json.dumps(output, indent=2) + "\n", encoding="utf-8"
    )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "qualified"), default="offline")
    parser.add_argument(
        "--artifacts", type=Path, default=ROOT / "artifacts" / "tool-platform-phase2"
    )
    arguments = parser.parse_args()
    result = run(ROOT, arguments.artifacts, mode=arguments.mode)
    print(
        json.dumps(
            {
                "offline_transport_tests_passed": result["offline_transport_tests_passed"],
                "application_full_checks": result["application_full_checks"],
                "phase2_fully_qualified": False,
                "stages": result["stages"],
            },
            indent=2,
        )
    )
    return 0 if result["offline_transport_tests_passed"] and arguments.mode == "offline" else 1


if __name__ == "__main__":
    raise SystemExit(main())
