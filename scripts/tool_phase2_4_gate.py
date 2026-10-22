#!/usr/bin/env python3
"""T02.4 offline/qualified API tool stage gate; never implies egress qualification."""

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
    "src/potbot/tools/api_contracts.py",
    "src/potbot/tools/api_openapi.py",
    "src/potbot/tools/api_http.py",
    "src/potbot/tools/api_grpc.py",
    "tests/unit/test_api_contracts.py",
    "tests/unit/test_api_openapi.py",
    "tests/unit/test_api_http.py",
    "tests/unit/test_api_grpc.py",
    "tests/unit/test_api_stage_gate.py",
    "tests/integration/test_tool_phase2_4_local_services.py",
    "examples/tool_api_demo.py",
    "scripts/tool_phase2_4_gate.py",
    "docs/implementation/04-http-api-adapters.md",
)
SELECTORS = (
    "tests/unit/test_api_contracts.py",
    "tests/unit/test_api_openapi.py",
    "tests/unit/test_api_http.py",
    "tests/unit/test_api_grpc.py",
    "tests/unit/test_api_stage_gate.py",
    "tests/integration/test_tool_phase2_4_local_services.py",
)


def suite_stats(file: Path) -> dict[str, int]:
    try:
        xml = ET.parse(file).getroot()
    except (OSError, ET.ParseError):
        return {"tests": 0, "failures": 0, "errors": 1, "skipped": 0}
    nodes = [xml] if xml.tag == "testsuite" else list(xml.iter("testsuite"))
    return {
        key: sum(int(node.attrib.get(key, "0")) for node in nodes)
        for key in ("tests", "failures", "errors", "skipped")
    }


def run(*, mode: str, root: Path, output: Path) -> dict[str, Any]:
    if mode not in {"offline", "qualified"}:
        raise ValueError("mode must be offline or qualified")
    root = root.resolve(strict=True)
    output = output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    fingerprints = {}
    for name in SOURCES:
        path = root / name
        fingerprints[name] = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file() and not path.is_symlink()
            else None
        )
    junit = output.with_suffix(".junit.xml")
    try:
        cmd = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *SELECTORS, f"--junitxml={junit}"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        exit_code = cmd.returncode
    except (OSError, subprocess.TimeoutExpired):
        exit_code = 124
    counts = suite_stats(junit)
    fixture_ok = (
        exit_code == 0 and counts["tests"] > 0 and counts["failures"] == counts["errors"] == 0
    )
    fully_tested = fixture_ok and counts["skipped"] == 0 and all(fingerprints.values())
    full_check_status: int | None = None
    if mode == "qualified" and fully_tested:
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts/check.py"),
                    "--with-postgres",
                    "--with-sandbox",
                    "--artifacts",
                    str(output.parent / "full-check"),
                ],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1800,
                check=False,
            )
            full_check_status = proc.returncode
        except (OSError, subprocess.TimeoutExpired):
            full_check_status = 124
    payload: dict[str, Any] = {
        "schema_version": 1,
        "stage": "T02.4",
        "mode": mode,
        "timestamp": datetime.now(UTC).isoformat(),
        "source_sha256": fingerprints,
        "fixture_counts": counts,
        "fixture_exit_code": exit_code,
        "fixtures_passed": fixture_ok,
        "all_real_service_fixtures_exercised": fully_tested,
        "full_repository_exit_code": full_check_status,
        "full_repository_passed": full_check_status == 0,
        "production_egress_qualified": False,
        "durable_api_dispatch_qualified": False,
        "stage_qualified": False,
        "qualification_note": "T03 must supply real enforced egress, credential custody and durable effect admission",
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "qualified"), default="offline")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "artifacts" / "tool-platform-t02-4" / "gate.json"
    )
    args = parser.parse_args()
    payload = run(mode=args.mode, root=args.root, output=args.output)
    print(
        json.dumps(
            {
                key: payload[key]
                for key in ("stage", "mode", "fixture_counts", "fixtures_passed", "stage_qualified")
            },
            sort_keys=True,
        )
    )
    if args.mode == "qualified":
        # No success until the T03 egress and durable-execution evidence exists.
        return 0 if payload["stage_qualified"] else 2
    return 0 if payload["fixtures_passed"] and all(payload["source_sha256"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
