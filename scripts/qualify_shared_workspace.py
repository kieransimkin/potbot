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
import re
import shlex
import sys
from pathlib import Path
from uuid import UUID, uuid4

from potbot.evaluation.sandbox_worker_acceptance import write_report
from potbot.tools.ai_config_history import AiConfigHistoryMonitor
from potbot.tools.gui_audit_verification import verify_gui_run
from potbot.tools.shared_workspace_isolation import (
    SharedWorkspaceBubblewrapBackend,
    SharedWorkspaceRuntime,
)
from potbot.tools.supervisor import ProcessRequest, ProcessSupervisor
from potbot.tools.worker_telemetry import WorkerMonitorConfig


def _case(supervisor: ProcessSupervisor, *, home: Path, command: str,
          must_succeed: bool, workspace_id: UUID, principal_id: UUID,
          audit_root: Path) -> dict[str, object]:
    # These are selected by the trusted operator, never by a guest request.
    if not isinstance(workspace_id, UUID) or not isinstance(principal_id, UUID):
        raise ValueError("smoke requires explicit operator workspace/principal UUIDs")
    if type(must_succeed) is not bool:
        raise ValueError("smoke expectation must be a literal boolean")
    run_id = uuid4()
    request = ProcessRequest(
        argv=("/bin/bash", "--noprofile", "--norc", "-c", command),
        cwd=home, workspace_root=home, sandbox_root=home,
        environment={"HOME": "/home"}, timeout_ms=15000,
        memory_bytes=1024**3, cpu_seconds=15,
        max_output_bytes=8192, max_file_bytes=4 * 1024**2,
        telemetry_workspace_id=workspace_id, telemetry_agent_id=principal_id,
        telemetry_identity_kind="principal", telemetry_run_id=run_id,
    )
    result = supervisor.run(request)
    # A denial counts only after actual dispatch, confirmed shutdown, intact
    # bounded capture, and independent readback of the complete kernel audit.
    trustworthy_run = (
        result.dispatched is True and result.processes_stopped is True
        and isinstance(result.isolation_identity, str)
        and re.fullmatch(r"[0-9a-f]{64}", result.isolation_identity) is not None
        and result.stdout_truncated is False and result.stderr_truncated is False
        and type(result.exit_code) is int
    )
    audit_verified = False
    if trustworthy_run:
        try:
            audit = verify_gui_run(
                audit_root, workspace_id, run_id,
                identity_kind="principal", subject_id=principal_id,
            )
            audit_verified = (
                audit.get("qualified") is True
                and audit.get("workspaceId") == str(workspace_id)
                and audit.get("runId") == str(run_id)
                and audit.get("identityKind") == "principal"
            )
        except (OSError, ValueError):
            # Never echo private transcript paths/content into the report.
            audit_verified = False
    if must_succeed:
        passed = (trustworthy_run and audit_verified and result.outcome == "completed"
                  and result.exit_code == 0)
    else:
        # These fixed write/socket probes use exit 1 for denial. A missing
        # executable (126/127), signal, or infrastructure failure is not denial.
        passed = (trustworthy_run and audit_verified and result.outcome == "nonzero_exit"
                  and result.exit_code == 1)
    return {
        "passed": passed, "outcome": result.outcome, "exit_code": result.exit_code,
        "run_id": str(run_id), "audit_verified": audit_verified,
        "dispatched": result.dispatched,
        "processes_stopped": result.processes_stopped,
        "isolation_identity": result.isolation_identity,
        "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
    }


def _network_namespace_command(host_namespace: str) -> str:
    if not isinstance(host_namespace, str) or not re.fullmatch(r"net:\[[0-9]+\]", host_namespace):
        raise ValueError("host network namespace identity is unavailable")
    # Empty output or a failed readlink must not compare unequal and pass.
    return (
        'guest_net_ns=$(readlink /proc/self/ns/net) && '
        '[[ "$guest_net_ns" =~ ^net:\\[[0-9]+\\]$ ]] && '
        'test "$guest_net_ns" != ' + shlex.quote(host_namespace)
    )


def _network_denial_command() -> str:
    # No DNS or outbound connection: require the seccomp-style denial of both
    # IPv4 and IPv6 socket creation. Missing Python/unsupported families and
    # unrelated OS errors must not look like a successful policy denial.
    program = """import errno, socket, sys
for family in (socket.AF_INET, socket.AF_INET6):
    try:
        with socket.socket(family, socket.SOCK_STREAM):
            pass
    except OSError as error:
        if error.errno not in (errno.EPERM, errno.EACCES):
            sys.exit(2)
    else:
        sys.exit(0)
sys.exit(1)
"""
    return "/usr/bin/python3 -I -S -c " + shlex.quote(program)


def _remove_marker(directory: Path, marker: str) -> None:
    """Unlink only the random probe entry, never follow a substituted directory."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        try:
            os.unlink(marker, dir_fd=fd)
        except FileNotFoundError:
            pass
    finally:
        os.close(fd)


def _report_destination(path: Path, runtime: SharedWorkspaceRuntime) -> Path:
    """Validate private, fresh output before running any probes; writer rechecks."""
    if not path.is_absolute() or path.resolve() != path or path.is_symlink() or path.exists():
        raise ValueError("report requires a fresh canonical absolute pathname")
    parent = path.parent
    info = parent.stat()
    if (os.name != "posix" or not parent.is_dir() or parent.resolve(strict=True) != parent
            or info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise ValueError("report parent must be an existing operator-private directory")
    for source in (
        runtime.rootfs, runtime.home, runtime.model_staging, runtime.shared_models,
        runtime.shared_tools, runtime.shared_skills, runtime.ai_config_dir, runtime.ai_history_dir,
    ):
        if source is not None and path.is_relative_to(source.resolve(strict=True)):
            raise ValueError("report must stay outside every guest mount")
    return path


def qualify(runtime: SharedWorkspaceRuntime,
            ai_monitor: AiConfigHistoryMonitor | None = None, *,
            workspace_id: UUID, principal_id: UUID,
            monitor_config: WorkerMonitorConfig | None = None) -> dict[str, object]:
    if not isinstance(workspace_id, UUID) or not isinstance(principal_id, UUID):
        raise ValueError("smoke requires explicit operator workspace/principal UUIDs")
    if (runtime.ai_config_dir is None) != (runtime.ai_history_dir is None):
        raise ValueError("AI configuration and history mounts must be paired")
    if runtime.ai_config_dir is not None and ai_monitor is None:
        raise RuntimeError("AI mounts require an operator history watcher")
    home = runtime.home.resolve(strict=True)
    config = monitor_config or WorkerMonitorConfig.from_environment()
    supervisor = ProcessSupervisor(
        SharedWorkspaceBubblewrapBackend(runtime, ai_monitor=ai_monitor), monitor_config=config,
    )

    def run_case(*, command: str, must_succeed: bool) -> dict[str, object]:
        result = _case(
            supervisor, home=home, command=command, must_succeed=must_succeed,
            workspace_id=workspace_id, principal_id=principal_id, audit_root=config.audit_root,
        )
        if result["processes_stopped"] is not True:
            # Do not launch another probe or remove possible live-worker files.
            raise RuntimeError("smoke process shutdown requires operator reconciliation")
        return result

    marker = "potbot-qualification-" + os.urandom(8).hex()
    # An outbound TCP failure is NOT proof of network isolation: an
    # unconfined process can fail to connect for unrelated reasons. Compare
    # the live kernel namespace IDs of controller and sandbox explicitly.
    host_net_ns = os.readlink("/proc/self/ns/net")
    tests = {
        "network_namespace_isolated": run_case(
            command=_network_namespace_command(host_net_ns),
            must_succeed=True,
        ),
        "bash_starts_at_home": run_case(
            command='test "$PWD" = /home && test "$HOME" = /home',
            must_succeed=True,
        ),
        "home_is_writable": run_case(
            command=f'touch /home/.tmp/{marker} && test -e /home/.tmp/{marker}',
            must_succeed=True,
        ),
        "ai_staging_writable": run_case(
            command=f'touch /var/ai/{marker} && test -e /var/ai/{marker}',
            must_succeed=True,
        ),
        "shared_tools_read_only_during_normal_run": run_case(
            command=f'touch /usr/local/tools/{marker}',
            must_succeed=False,
        ),
        "shared_tools_group_mode": run_case(
            command='test "$(stat -c %a /usr/local/tools)" = 2775 && '
                    'test "$(stat -c %g /usr/local/tools)" = 69',
            must_succeed=True,
        ),
        "shared_models_read_only": run_case(
            command=f'touch /opt/potbot/models/{marker}', must_succeed=False,
        ),
        "model_cache_env_routed": run_case(
            command='test "$HF_HOME" = /var/ai/cache/huggingface && '
                    'test "$OLLAMA_MODELS" = /var/ai/cache/ollama/models',
            must_succeed=True,
        ),
        "etc_is_read_only": run_case(
            command=f'touch /etc/{marker}', must_succeed=False,
        ),
        "windows_drive_not_visible": run_case(
            command='test ! -e /mnt/c/Windows && test ! -e /mnt/d',
            must_succeed=True,
        ),
        "network_is_denied": run_case(
            command=_network_denial_command(),
            must_succeed=False,
        ),
    }
    if runtime.ai_config_dir is not None and runtime.ai_history_dir is not None:
        assert ai_monitor is not None  # Required before any dispatch above.
        tests["etc_ai_writable"] = run_case(
            command=f'touch /etc/ai/{marker} && test -e /etc/ai/{marker}',
            must_succeed=True,
        )
        tests["etc_ai_past_readonly"] = run_case(
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
        _remove_marker(runtime.ai_config_dir, marker)
        ai_monitor.poll_once()
    _remove_marker(home / ".tmp", marker)
    if runtime.model_staging is not None:
        _remove_marker(runtime.model_staging, marker)
    passed = all(item["passed"] is True for item in tests.values())
    result: dict[str, object] = {
        "schema": "potbot-sandbox-qualification-smoke/v2", "qualified_smoke": passed,
        "platform": platform.platform(), "image_digest": runtime.image_digest,
        "workspace_id": str(workspace_id), "principal_id": str(principal_id),
        "qualification_id": str(uuid4()), "identity_kind": "principal",
        "qualifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "tests": tests,
        "warning": "Live smoke only, not complete hostile-code/Windows isolation attestation",
    }
    result["receipt_sha256"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return result


def main(argv: list[str] | None = None) -> int:
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
    parser.add_argument("--workspace-id", type=UUID, required=True)
    parser.add_argument("--principal-id", type=UUID, required=True)
    args = parser.parse_args(argv)
    if (args.ai_config is None) != (args.ai_past is None):
        parser.error("--ai-config and --ai-past must be supplied together")
    try:
        runtime = SharedWorkspaceRuntime(
            args.image_digest, args.rootfs, args.home, "0" * 64,
            model_staging=args.model_staging, shared_models=args.shared_models,
            shared_tools=args.shared_tools, ai_config_dir=args.ai_config,
            ai_history_dir=args.ai_past,
        )
        report_path = _report_destination(args.report, runtime)
        config = WorkerMonitorConfig.from_environment()
        if args.ai_config is not None:
            with AiConfigHistoryMonitor(config_dir=args.ai_config, past_dir=args.ai_past) as monitor:
                report = qualify(
                    runtime, ai_monitor=monitor, workspace_id=args.workspace_id,
                    principal_id=args.principal_id, monitor_config=config,
                )
        else:
            report = qualify(
                runtime, workspace_id=args.workspace_id, principal_id=args.principal_id,
                monitor_config=config,
            )
        write_report(report_path, report)
    except Exception as exc:
        print("sandbox qualification could not execute: " + type(exc).__name__, file=sys.stderr)
        return 2
    print(json.dumps({"qualified_smoke": report["qualified_smoke"],
                      "receipt_sha256": report["receipt_sha256"]}))
    return 0 if report["qualified_smoke"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
