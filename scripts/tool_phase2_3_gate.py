#!/usr/bin/env python3
"""T02.3 gate: unit fixture evidence never masquerades as qualified sandbox evidence.

Use `--mode qualified` on a provisioned Linux CI worker with real PostgreSQL,
Bubblewrap/seccomp, and the pinned uv environment. The full repo verification
is authoritative; this script emits compact machine-readable stage evidence.
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
SOURCES = (
    "src/potbot/tools/cli_adapter.py",
    "src/potbot/tools/interactive_supervisor.py",
    "src/potbot/tools/mcp_sandbox.py",
    "tests/unit/test_cli_adapter.py",
    "tests/unit/test_interactive_supervisor.py",
    "tests/unit/test_mcp_sandbox.py",
    "tests/integration/test_tool_phase2_3_sandbox.py",
)
UNIT_SELECTORS = (
    "tests/unit/test_cli_adapter.py",
    "tests/unit/test_interactive_supervisor.py",
    "tests/unit/test_mcp_sandbox.py",
)
SANDBOX_SELECTORS = ("tests/integration/test_tool_phase2_3_sandbox.py",)


def counts(path: Path) -> dict[str, int]:
    empty = {"tests": 0, "failures": 0, "errors": 1, "skipped": 0}
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return empty
    suites = (root,) if root.tag == "testsuite" else tuple(root.iter("testsuite"))
    return {
        field: sum(int(row.get(field, "0")) for row in (s.attrib for s in suites))
        for field in ("tests", "failures", "errors", "skipped")
    }


def _pytest(root: Path, report: Path, selectors: tuple[str, ...], *, timeout: int) -> dict[str, Any]:
    try:
        process = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *selectors, f"--junitxml={report}"],
            cwd=root, capture_output=True, text=True, timeout=timeout, check=False,
        )
        exit_code = process.returncode
    except (OSError, subprocess.TimeoutExpired):
        exit_code = 124
    metrics = counts(report)
    return {
        "exit_code": exit_code,
        "test_counts": metrics,
        "passed": exit_code == 0 and metrics["tests"] > 0 and
        metrics["skipped"] == metrics["failures"] == metrics["errors"] == 0,
    }


def run(root: Path, artifact_root: Path, *, mode: str = "offline") -> dict[str, Any]:
    if mode not in {"offline", "qualified"}:
        raise ValueError("Unknown phase 2.3 qualification mode")
    root = root.resolve(strict=True)
    artifact_root.mkdir(parents=True, exist_ok=True)
    digests: dict[str, str | None] = {}
    for relative in SOURCES:
        path = root / relative
        digests[relative] = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file() and not path.is_symlink() else None
        )
    offline = _pytest(root, artifact_root / "t02-3-unit.xml", UNIT_SELECTORS, timeout=240)
    qualified_sandbox: dict[str, Any] | None = None
    complete_application = "not_run"
    if mode == "qualified" and offline["passed"] and all(digests.values()):
        qualified_sandbox = _pytest(
            root, artifact_root / "t02-3-sandbox.xml", SANDBOX_SELECTORS, timeout=300
        )
        if qualified_sandbox["passed"]:
            full_dir = artifact_root / "full-repository"
            full_dir.mkdir(exist_ok=True)
            try:
                result = subprocess.run(
                    [sys.executable, str(root / "scripts/check.py"),
                     "--with-postgres", "--with-sandbox", "--artifacts", str(full_dir)],
                    cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False, timeout=1800,
                )
                summary_file = full_dir / "summary.json"
                summary = json.loads(summary_file.read_text()) if summary_file.is_file() else {}
                complete_application = "passed" if (
                    result.returncode == 0 and summary.get("success") is True
                    and summary.get("postgres_requested") is True
                    and summary.get("sandbox_requested") is True
                ) else "failed"
            except (OSError, subprocess.TimeoutExpired):
                complete_application = "failed"
    qualified = (
        all(digests.values()) and offline["passed"] and qualified_sandbox is not None
        and qualified_sandbox["passed"] and complete_application == "passed"
    )
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True, timeout=4,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sha = None
    output = {
        "schema_version": 1,
        "stage": "T02.3",
        "timestamp": datetime.now(UTC).isoformat(),
        "repository_head": sha,
        "mode": mode,
        "offline_passed": bool(offline["passed"] and all(digests.values())),
        "sandbox_suite": qualified_sandbox,
        "full_application_checks": complete_application,
        "fully_qualified": bool(qualified),
        "unit_suite": offline,
        "source_sha256": digests,
        "outstanding_gates": [] if qualified else [
            "Qualified Linux Bubblewrap/seccomp interactive execution and full PostgreSQL CI",
            "Controller-owned durable tool dispatch/lease/budget/side-effect reconciliation (T03)",
            "Production credential and network egress controls remain disabled",
        ],
    }
    (artifact_root / "t02-3-stage-gate.json").write_text(json.dumps(output, indent=2) + "\n")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "qualified"), default="offline")
    parser.add_argument(
        "--artifacts", type=Path, default=ROOT / "artifacts" / "tool-platform-t02-3"
    )
    args = parser.parse_args()
    result = run(ROOT, args.artifacts, mode=args.mode)
    print(json.dumps({
        "stage": result["stage"], "offline_passed": result["offline_passed"],
        "fully_qualified": result["fully_qualified"],
        "unit_tests": result["unit_suite"]["test_counts"],
    }, indent=2))
    return 0 if (result["fully_qualified"] if args.mode == "qualified" else result["offline_passed"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
