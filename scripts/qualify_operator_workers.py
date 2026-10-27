"""Fail-closed release gate for monitored, sandboxed Potbot Cog workers.

Run on the target Linux operator host with its actual credentials, configured
cgroup delegation and sandbox image. Offline unit tests are not a substitute.
This runner does not alter configuration or grant elevated capabilities.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


REQUIRED = (
    "POTBOT_OPERATOR_AUDIT_ROOT",
    "POTBOT_OPERATOR_CGROUP_ROOT",
    "POTBOT_OPERATOR_STRACE",
    "POTBOT_OPERATOR_STRACE_SHA256",
    "POTBOT_OPERATOR_CGROUP_JOIN",
    "POTBOT_OPERATOR_CGROUP_JOIN_SHA256",
)


def run_command(argv: list[str], *, timeout: int, success_field: str = "qualified") -> dict[str, object]:
    """Require an explicit machine-readable attestation; exit zero is insufficient."""
    started = time.monotonic()
    try:
        process = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=timeout, check=False)
        # Never print child output: it may contain private process/file details.
        lines = process.stdout.strip().splitlines()
        report = json.loads(lines[-1]) if len(lines) == 1 else None
        qualified = (process.returncode == 0 and type(report) is dict
                     and report.get(success_field) is True)
        return {"passed": qualified, "exit_code": process.returncode,
                "elapsed_ms": int((time.monotonic() - started) * 1000)}
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        return {"passed": False, "error": type(error).__name__}


def inspect_host() -> dict[str, object]:
    missing = sorted(key for key in REQUIRED if not os.getenv(key))
    linux = sys.platform == "linux"
    cgroup = Path("/sys/fs/cgroup/cgroup.controllers").is_file() if linux else False
    return {"passed": linux and cgroup and not missing,
            "linux": linux, "cgroup_v2": cgroup, "missing_settings": missing}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable,
                        help="Interpreter from the installed operator environment")
    parser.add_argument("--timeout", type=int, default=90)
    for name in ("rootfs", "home", "model-staging", "shared-models", "shared-tools", "image-digest", "qualification-sha256", "report"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if not 5 <= args.timeout <= 3600:
        parser.error("timeout must be between 5 and 3600 seconds")
    # The report is operator-private evidence, never an artefact in guest /home.
    home, report = Path(args.home).resolve(), Path(args.report).resolve()
    if report.is_relative_to(home):
        parser.error("report must be outside the sandbox home")
    if not report.parent.is_dir() or report.parent.is_symlink():
        parser.error("report parent must be an existing operator-owned directory")
    checks: dict[str, dict[str, object]] = {"host": inspect_host()}
    if checks["host"]["passed"]:
        checks["taskstats"] = run_command(
            [args.python, "scripts/qualify_worker_exit_accounting.py"],
            timeout=args.timeout,
        )
        # Run the integration command only after the kernel gate succeeds.
        if checks["taskstats"]["passed"]:
            smoke_args = ["--rootfs", args.rootfs, "--home", args.home,
                          "--model-staging", args.model_staging,
                          "--shared-models", args.shared_models,
                          "--shared-tools", args.shared_tools,
                          "--image-digest", args.image_digest,
                          "--qualification-sha256", args.qualification_sha256,
                          "--report", args.report]
            checks["sandbox_integration"] = run_command(
                [args.python, "scripts/qualify_phase35_worker.py", *smoke_args],
                timeout=args.timeout, success_field="qualified_smoke")
    qualified = all(check.get("passed") is True for check in checks.values()) and (
        set(checks) == {"host", "taskstats", "sandbox_integration"})
    final = {"schema": 1, "qualified": qualified, "checks": checks}
    # Persist only a summary and refuse unsafe pre-existing paths. A separate
    # Phase 35 full report is written by the existing worker qualification.
    summary = report.with_name(report.stem + ".operator-summary.json")
    try:
        fd = os.open(summary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(final, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        final["qualified"] = False
        qualified = False
    print(json.dumps(final, sort_keys=True))
    return 0 if qualified else 1


if __name__ == "__main__":
    raise SystemExit(main())
