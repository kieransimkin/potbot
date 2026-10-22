#!/usr/bin/env python3
"""T02.4 gate with explicit fixture diagnostics; never asserts production authorization.

Each execution writes a sanitized pytest transcript, JUnit XML and a bounded JSON
summary. Skipped tests are not success. A distinct workspace-local --basetemp
isolates pytest's fixtures from the user's Windows global temporary directory.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from potbot.evaluation.diagnostics import sanitize_text

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
_MAX_TEST_LOG = 256 * 1024
_MAX_ISSUE_TEXT = 2400
_MAX_ISSUES = 32


def suite_stats(file: Path) -> dict[str, int]:
    """Count JUnit cases without ever treating missing/unparseable reports as success."""
    try:
        xml = ET.parse(file).getroot()
    except (OSError, ET.ParseError):
        return {"tests": 0, "failures": 0, "errors": 1, "skipped": 0}
    nodes = [xml] if xml.tag == "testsuite" else list(xml.iter("testsuite"))
    return {
        key: sum(int(node.attrib.get(key, "0")) for node in nodes)
        for key in ("tests", "failures", "errors", "skipped")
    }


def suite_findings(file: Path) -> list[dict[str, str]]:
    """Extract bounded names and diagnostics for pytest failures and skips."""
    try:
        xml = ET.parse(file).getroot()
    except (OSError, ET.ParseError):
        return [
            {"test": "pytest", "status": "error", "message": "JUnit report is missing or invalid"}
        ]
    findings: list[dict[str, str]] = []
    for case in xml.iter("testcase"):
        name = str(case.attrib.get("name", "<unnamed>"))[:256]
        group = str(case.attrib.get("classname", "<unknown>"))[:256]
        for child in case:
            if child.tag not in {"error", "failure", "skipped"}:
                continue
            excerpt = str(child.get("message", "")) + "\n" + (child.text or "")
            findings.append(
                {
                    "test": sanitize_text(f"{group}::{name}")[:512],
                    "status": child.tag,
                    "message": sanitize_text(excerpt.strip())[:_MAX_ISSUE_TEXT],
                }
            )
            if len(findings) >= _MAX_ISSUES:
                return findings
    return findings


def integration_modules() -> dict[str, bool]:
    """Report optional test packages; never silently weaken integration coverage."""
    installed: dict[str, bool] = {}
    for name in ("grpc", "google.protobuf"):
        try:
            installed[name] = importlib.util.find_spec(name) is not None
        except (ImportError, AttributeError, ValueError):
            installed[name] = False
    return installed


def _test_run(root: Path, output: Path) -> tuple[int, Path, Path, str]:
    junit = output.with_suffix(".junit.xml")
    log_file = output.with_suffix(".pytest.log")
    # A failed launcher must never inherit a previous run's passing XML.
    junit.unlink(missing_ok=True)
    temp_parent = root / ".test-runs"
    temp_parent.mkdir(parents=True, exist_ok=True)
    temp_dir = temp_parent / f"t02-4-pytest-{uuid4().hex[:12]}"
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-ra",
                "--tb=short",
                f"--basetemp={temp_dir}",
                *SELECTORS,
                f"--junitxml={junit}",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=300,
            check=False,
        )
        exit_code = result.returncode
        diagnostic = result.stdout + "\n" + result.stderr
    except subprocess.TimeoutExpired as error:
        exit_code = 124
        diagnostic = "T02.4 pytest exceeded the five-minute deadline.\n"
        for chunk in (error.stdout, error.stderr):
            if chunk is not None:
                diagnostic += (
                    chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk
                )
    except OSError as error:
        exit_code = 127
        diagnostic = f"Unable to launch pytest: {type(error).__name__}: {error}\n"
    safe = sanitize_text(diagnostic[:_MAX_TEST_LOG])
    log_file.write_text(safe, encoding="utf-8")
    # Preserve valid XML while redacting known secrets in attributes and text.
    if junit.is_file():
        report = junit.read_text(encoding="utf-8", errors="replace")
        redacted = sanitize_text(report)
        if redacted != report:
            junit.write_text(redacted, encoding="utf-8")
    return exit_code, junit, log_file, safe


def run(*, mode: str, root: Path, output: Path) -> dict[str, Any]:
    if mode not in {"offline", "qualified"}:
        raise ValueError("mode must be offline or qualified")
    root = root.resolve(strict=True)
    output = output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    fingerprints: dict[str, str | None] = {}
    for name in SOURCES:
        path = root / name
        fingerprints[name] = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file() and not path.is_symlink()
            else None
        )

    exit_code, junit, log_file, _ = _test_run(root, output)
    counts = suite_stats(junit)
    findings = suite_findings(junit)
    # Missing gRPC/protobuf are missing coverage, not a green optional success.
    fixture_ok = (
        exit_code == 0
        and counts["tests"] > 0
        and counts["failures"] == counts["errors"] == counts["skipped"] == 0
    )
    fully_tested = fixture_ok and all(fingerprints.values())
    full_check_status: int | None = None
    if mode == "qualified" and fully_tested:
        try:
            check = subprocess.run(
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
            full_check_status = check.returncode
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
        "test_findings": findings,
        "optional_integration_modules": integration_modules(),
        "pytest_log": str(log_file),
        "pytest_junit": str(junit),
        "pytest_temp_directory": "workspace/.test-runs/t02-4-pytest-*",
        "all_real_service_fixtures_exercised": fully_tested,
        "full_repository_exit_code": full_check_status,
        "full_repository_passed": full_check_status == 0,
        "production_egress_qualified": False,
        "durable_api_dispatch_qualified": False,
        "stage_qualified": False,
        "qualification_note": (
            "Offline fixture success is not production qualification; "
            "T03 must supply enforced egress, credential custody and durable effect admission"
        ),
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "qualified"), default="offline")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "artifacts" / "tool-platform-t02-4" / "gate.json",
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
    if not payload["fixtures_passed"]:
        print("T02.4 test failures and skips:", file=sys.stderr)
        for finding in payload["test_findings"]:
            message = finding["message"].splitlines()[0] if finding["message"] else ""
            print(f"  {finding['status']}: {finding['test']}: {message}", file=sys.stderr)
        if any(not present for present in payload["optional_integration_modules"].values()):
            print(
                "Missing optional integration-test modules. Add grpcio and protobuf "
                "as development dependencies, then update uv.lock.",
                file=sys.stderr,
            )
        print(f"Full sanitized pytest diagnostics: {payload['pytest_log']}", file=sys.stderr)
    if args.mode == "qualified":
        # T03 authorization/egress is not implemented: cannot declare this qualified.
        return 0 if payload["stage_qualified"] else 2
    return 0 if payload["fixtures_passed"] and all(payload["source_sha256"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
