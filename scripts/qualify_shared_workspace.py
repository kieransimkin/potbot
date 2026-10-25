#!/usr/bin/env python3
"""Run REAL sandbox smoke tests on a provisioned Linux/WSL2 host. No mocks.

The smoke is only one prerequisite for a full adversarial host qualification.
The operator must bind the resulting receipt to the unpacked, signed OCI image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import sys
from pathlib import Path

from potbot.tools.ai_config_history import AiConfigHistoryMonitor
from potbot.tools.shared_workspace_isolation import (
    SharedWorkspaceBubblewrapBackend,
    SharedWorkspaceRuntime,
)
from potbot.tools.supervisor import ProcessRequest, ProcessSupervisor


def _case(supervisor: ProcessSupervisor, *, home: Path, command: str,
          must_succeed: bool) -> dict[str, object]:
    request = ProcessRequest(
        argv=("/bin/bash", "--noprofile", "--norc", "-c", command),
        cwd=home, workspace_root=home, sandbox_root=home,
        environment={"HOME": "/home"}, timeout_ms=15000,
        memory_bytes=1024**3, cpu_seconds=15,
        max_output_bytes=8192, max_file_bytes=4 * 1024**2,
    )
    result = supervisor.run(request)
    # A failed command is not evidence of read-only or network isolation if
    # Bubblewrap failed to dispatch it, the process was left running, or the
    # operator has no isolation identity to bind to the receipt.
    trustworthy_run = (result.dispatched and result.processes_stopped
                       and bool(result.isolation_identity))
    if must_succeed:
        passed = (trustworthy_run and result.outcome == "completed"
                  and result.exit_code == 0)
    else:
        passed = (trustworthy_run and result.outcome == "nonzero_exit"
                  and result.exit_code not in (None, 0))
    return {
        "passed": passed, "outcome": result.outcome, "exit_code": result.exit_code,
        "dispatched": result.dispatched,
        "processes_stopped": result.processes_stopped,
        "isolation_identity": result.isolation_identity,
        "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
    }


def qualify(runtime: SharedWorkspaceRuntime,
            ai_monitor: AiConfigHistoryMonitor | None = None) -> dict[str, object]:
    home = runtime.home.resolve(strict=True)
    supervisor = ProcessSupervisor(SharedWorkspaceBubblewrapBackend(runtime, ai_monitor=ai_monitor))
    marker = "potbot-qualification-" + os.urandom(8).hex()
    # An outbound TCP failure is NOT proof of network isolation: an
    # unconfined process can fail to connect for unrelated reasons. Compare
    # the live kernel namespace IDs of controller and sandbox explicitly.
    host_net_ns = os.readlink("/proc/self/ns/net")
    if not host_net_ns.startswith("net:[") or not host_net_ns.endswith("]"):
        raise RuntimeError("host network namespace identity is unavailable")
    tests = {
        "network_namespace_isolated": _case(
            supervisor, home=home,
            command='test "$(readlink /proc/self/ns/net)" != '
                    + shlex.quote(host_net_ns),
            must_succeed=True,
        ),
        "bash_starts_at_home": _case(
            supervisor, home=home, command='test "$PWD" = /home && test "$HOME" = /home',
            must_succeed=True,
        ),
        "home_is_writable": _case(
            supervisor, home=home, command=f'touch /home/.tmp/{marker} && test -e /home/.tmp/{marker}',
            must_succeed=True,
        ),
        "ai_staging_writable": _case(
            supervisor, home=home,
            command=f'touch /var/ai/{marker} && test -e /var/ai/{marker}',
            must_succeed=True,
        ),
        "shared_tools_read_only_during_normal_run": _case(
            supervisor, home=home,
            command=f'touch /usr/local/tools/{marker}',
            must_succeed=False,
        ),
        "shared_tools_group_mode": _case(
            supervisor, home=home,
            command='test "$(stat -c %a /usr/local/tools)" = 2775 && '
                    'test "$(stat -c %g /usr/local/tools)" = 69',
            must_succeed=True,
        ),
        "shared_models_read_only": _case(
            supervisor, home=home,
            command=f'touch /opt/potbot/models/{marker}', must_succeed=False,
        ),
        "model_cache_env_routed": _case(
            supervisor, home=home,
            command='test "$HF_HOME" = /var/ai/cache/huggingface && '
                    'test "$OLLAMA_MODELS" = /var/ai/cache/ollama/models',
            must_succeed=True,
        ),
        "etc_is_read_only": _case(
            supervisor, home=home, command=f'touch /etc/{marker}', must_succeed=False,
        ),
        "windows_drive_not_visible": _case(
            supervisor, home=home, command='test ! -e /mnt/c/Windows && test ! -e /mnt/d',
            must_succeed=True,
        ),
        "network_is_denied": _case(
            supervisor, home=home, command='echo forbidden > /dev/tcp/1.1.1.1/443',
            must_succeed=False,
        ),
    }
    if runtime.ai_config_dir is not None and runtime.ai_history_dir is not None:
        if ai_monitor is None:
            raise RuntimeError("AI mounts require an operator history watcher")
        tests["etc_ai_writable"] = _case(
            supervisor, home=home,
            command=f'touch /etc/ai/{marker} && test -e /etc/ai/{marker}',
            must_succeed=True,
        )
        tests["etc_ai_past_readonly"] = _case(
            supervisor, home=home,
            command=f'touch /etc/ai-past/{marker}', must_succeed=False,
        )
        # The operator records both file creation and later removal. The marker
        # must be present in a durable inverse record even if its inotify event
        # has already been consumed by the monitor's background thread.
        ai_monitor.poll_once()
        history_files = sorted(runtime.ai_history_dir.glob("*.reverse.json"))
        recorded = any(marker in path.read_text(encoding="utf-8")
                       for path in history_files[-32:])
        tests["etc_ai_reverse_recorded"] = {"passed": recorded, "records": len(history_files)}
        (runtime.ai_config_dir / marker).unlink(missing_ok=True)
        ai_monitor.poll_once()
    (home / ".tmp" / marker).unlink(missing_ok=True)
    (runtime.model_staging / marker).unlink(missing_ok=True)
    passed = all(item["passed"] for item in tests.values())
    result: dict[str, object] = {
        "schema": "potbot-sandbox-qualification-smoke/v1", "qualified_smoke": passed,
        "platform": platform.platform(), "image_digest": runtime.image_digest,
        "tests": tests,
        "warning": "Live smoke only, not complete hostile-code/Windows isolation attestation",
    }
    result["receipt_sha256"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rootfs", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--model-staging", type=Path, required=True)
    parser.add_argument("--shared-models", type=Path, required=True)
    parser.add_argument("--shared-tools", type=Path, required=True)
    parser.add_argument("--ai-config", type=Path)
    parser.add_argument("--ai-past", type=Path)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if (args.ai_config is None) != (args.ai_past is None):
        parser.error("--ai-config and --ai-past must be supplied together")
    runtime = SharedWorkspaceRuntime(
        args.image_digest, args.rootfs, args.home, "0" * 64,
        model_staging=args.model_staging, shared_models=args.shared_models,
        shared_tools=args.shared_tools, ai_config_dir=args.ai_config,
        ai_history_dir=args.ai_past,
    )
    try:
        if args.ai_config is not None:
            with AiConfigHistoryMonitor(config_dir=args.ai_config, past_dir=args.ai_past) as monitor:
                report = qualify(runtime, ai_monitor=monitor)
        else:
            report = qualify(runtime)
    except Exception as exc:
        print("sandbox qualification could not execute: " + type(exc).__name__, file=sys.stderr)
        return 2
    report_path = args.report.resolve()
    if report_path.is_relative_to(args.home.resolve()):
        print("qualification report must be stored on the controller, not in /home", file=sys.stderr)
        return 2
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"qualified_smoke": report["qualified_smoke"],
                      "receipt_sha256": report["receipt_sha256"]}))
    return 0 if report["qualified_smoke"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
