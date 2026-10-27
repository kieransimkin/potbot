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
    started = time.monotonic()
    try:
        process = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=timeout, check=False)
        output = process.stdout.strip().splitlines()
        # The child may output PIDs and other sensitive details; never emit raw
        # stdout/stderr through the qualification report.
        qualified = process.returncode == 0
        if output:
            try:
                report = json.loads(output[-1])
                if not isinstance(report, dict) or report.get(success_field) is not True:
                    qualified = False
            except json.JSONDecodeError:
                pass
        return {"passed": qualified, "exit_code": process.returncode,
                "elapsed_ms": int((time.monotonic() - started) * 1000)}
    except (OSError, subprocess.TimeoutExpired) as error:
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
    print(json.dumps({"schema": 1, "qualified": qualified, "checks": checks},
                     sort_keys=True))
    return 0 if qualified else 1


if __name__ == "__main__":
    raise SystemExit(main())
