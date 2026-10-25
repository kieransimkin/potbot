#!/usr/bin/env python3
"""Live Phase 35 worker audit smoke: real Bubblewrap + cgroup + fork + taskstats.

This is an operator-only qualification, not a mock or a production attestation.
All monitor paths and their backing controls are kept outside the guest /home.
Exit 1 on missing taskstats evidence, unconfirmed cleanup or absent traces.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

from potbot.tools.shared_workspace_isolation import SharedWorkspaceBubblewrapBackend, SharedWorkspaceRuntime
from potbot.tools.supervisor import ProcessRequest, ProcessSupervisor
from potbot.tools.worker_telemetry import WorkerMonitorConfig, _safe_read


def inspect_result(result: object, lifetime: object) -> dict[str, object]:
    """Only explicitly evidenced process-tree completion qualifies."""
    record = lifetime if isinstance(lifetime, dict) else {}
    count = record.get("per_pid_kernel_exit_count")
    verified = (
        bool(getattr(result, "dispatched", False))
        and bool(getattr(result, "processes_stopped", False))
        and getattr(result, "outcome", None) == "completed"
        and getattr(result, "exit_code", None) == 0
        and bool(getattr(result, "isolation_identity", None))
        and record.get("audit_complete") is True
        and record.get("per_pid_kernel_exit_accounting") is True
        and type(count) is int and count >= 3
        and record.get("trace_integrity") == {"unpaired_syscalls": 0}
    )
    # Preserve error categories, not worker output, syscall arguments or paths.
    return {"schema": "potbot-phase35-live-worker-qualification/v1",
            "qualified_smoke": verified,
            "kernel_exit_tasks": count if type(count) is int else None,
            "isolation_established": bool(getattr(result, "isolation_identity", None)),
            "parent_and_descendants_stopped": bool(getattr(result, "processes_stopped", False)),
            "audited": record.get("audit_complete") is True}


def qualify(runtime: SharedWorkspaceRuntime, config: WorkerMonitorConfig) -> dict[str, object]:
    workspace, agent, run_id = uuid4(), uuid4(), uuid4()
    supervisor = ProcessSupervisor(SharedWorkspaceBubblewrapBackend(runtime), monitor_config=config)
    # Children are guaranteed to invoke actual file operations; strace and
    # kernel task-exit collection must cover bash + its subshells and utilities.
    statement = (
        'set -eu; printf hello > /home/.tmp/potbot-phase35-a; '
        '(cat /home/.tmp/potbot-phase35-a >/dev/null) & '
        '(head -c 16 /dev/zero >/home/.tmp/potbot-phase35-b) & wait; '
        'rm -f /home/.tmp/potbot-phase35-a /home/.tmp/potbot-phase35-b'
    )
    request = ProcessRequest(
        argv=("/bin/bash", "--noprofile", "--norc", "-c", statement),
        cwd=runtime.home, workspace_root=runtime.home, sandbox_root=runtime.home,
        timeout_ms=20000, cpu_seconds=20, memory_bytes=1024 ** 3,
        max_output_bytes=8192, max_file_bytes=8 * 1024 * 1024,
        telemetry_workspace_id=workspace, telemetry_agent_id=agent,
        telemetry_identity_kind="principal", telemetry_run_id=run_id,
    )
    result = supervisor.run(request)
    # Only the operator writes this location; no guest-supplied path is read.
    workspace_dir = config.audit_root / str(workspace)
    lifetime: object = None
    if workspace_dir.exists():
        for child in workspace_dir.iterdir():
            if child.is_dir() and child.name != ".active":
                path = child / "lifetime.json"
                if path.is_file() and not path.is_symlink():
                    candidate = _safe_read(path)
                    if candidate.get("run_id") == str(run_id):
                        lifetime = candidate
                        break
    return inspect_result(result, lifetime)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rootfs", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--model-staging", type=Path, required=True)
    parser.add_argument("--shared-models", type=Path, required=True)
    parser.add_argument("--shared-tools", type=Path, required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--qualification-sha256", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = WorkerMonitorConfig.from_environment()
        runtime = SharedWorkspaceRuntime(
            args.image_digest, args.rootfs, args.home, args.qualification_sha256,
            model_staging=args.model_staging, shared_models=args.shared_models,
            shared_tools=args.shared_tools,
        )
        if args.report.resolve().is_relative_to(args.home.resolve()):
            raise ValueError("qualification report cannot be stored in sandbox /home")
        report = qualify(runtime, config)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        print(json.dumps({"qualified_smoke": False, "error": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0 if report["qualified_smoke"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
