#!/usr/bin/env python3
"""Explicit, fail-closed smoke of a registered Cog worker through the real executor.

No automatic image admission, agent registration, privilege changes or fallback
executor. Existing operator configuration and kernel auditing are prerequisites.
The receipt does NOT qualify nested workflow RPC, PostgreSQL or Xpra browsers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

from potbot.evaluation.sandbox_worker_acceptance import (
    GUEST_EXECUTABLE,
    INPUT,
    OPERATION,
    OUTPUT,
    SCHEMA,
    check_audit,
    check_probe,
    check_process,
    digest,
    policy,
    read_private,
    report,
    unique_json,
    write_report,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    "sandbox/qualification/worker_probe.py",
    "scripts/qualify_sandbox_cog_s46.py",
    "src/potbot/evaluation/sandbox_worker_acceptance.py",
    "src/potbot/tools/sandbox_cog_worker.py",
    "src/potbot/tools/supervisor.py",
    "src/potbot/tools/shared_workspace_isolation.py",
    "src/potbot/tools/isolation.py",
    "src/potbot/tools/workspace_supervision.py",
    "src/potbot/tools/workspace_concurrency.py",
    "src/potbot/tools/agent_homes.py",
    "src/potbot/tools/worker_telemetry.py",
    "src/potbot/tools/gui_audit_verification.py",
    "src/potbot/tools/syn_worker_nodes.py",
)


def committed_sources(root: Path = ROOT) -> tuple[str, dict[str, str]]:
    def git(*argv):
        return subprocess.check_output(
            ["git", *argv], cwd=root, timeout=10, stderr=subprocess.DEVNULL
        )

    head = git("rev-parse", "HEAD").decode("ascii").strip()
    hashes = {}
    for name in SOURCES:
        committed = git("show", f"{head}:{name}")
        actual = root / name
        if actual.is_symlink() or actual.read_bytes() != committed:
            raise ValueError("qualification source differs from committed source")
        hashes[name] = digest(committed)
    return head, hashes


def _outside_guest(path: Path, config: dict) -> None:
    real = path.resolve(strict=False)
    for key, value in config["runtime"].items():
        if key.endswith("digest") or key == "qualification_sha256":
            continue
        mount = Path(value).resolve(strict=True)
        if real == mount or real.is_relative_to(mount) or mount.is_relative_to(real):
            raise ValueError("operator control/output overlaps a guest mount")


async def qualify(config: dict, head: str, hashes: dict[str, str]) -> dict:
    # Imports stay lazy: policy/schema tests do not masquerade as a live host.
    from potbot import __version__
    from potbot.cogs.contracts import CogRunContext, TypedCogValue
    from potbot.domain.auth import WorkspaceContext
    from potbot.domain.serialization import content_hash
    from potbot.tools.agent_homes import AgentHomeRegistry
    from potbot.tools.execution_contracts import ExecutionPolicy, WorkBinding
    from potbot.tools.gui_audit_verification import verify_gui_run
    from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor, SandboxedCogOperation
    from potbot.tools.shared_workspace_isolation import (
        SharedWorkspaceBubblewrapBackend,
        SharedWorkspaceRuntime,
    )
    from potbot.tools.supervisor import ProcessSupervisor
    from potbot.tools.syn_worker_nodes import SynWorkerNodeMesh
    from potbot.tools.worker_telemetry import WorkerMonitorConfig
    from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger
    from potbot.tools.workspace_supervision import validate_runtime_pin

    if sys.platform != "linux":
        raise ValueError("qualified Linux/WSL2 host required")
    runtime_fields = dict(config["runtime"])
    for key in runtime_fields:
        if key not in {"image_digest", "qualification_sha256"}:
            runtime_fields[key] = Path(runtime_fields[key])
            if runtime_fields[key].resolve(strict=True) != runtime_fields[key]:
                raise ValueError("runtime path redirected")
    runtime = SharedWorkspaceRuntime(**runtime_fields)
    workspace, principal = UUID(config["workspace_id"]), UUID(config["principal_id"])
    actor = WorkspaceContext(workspace, principal)
    ledger_path = Path(config["ledger"])
    # Never create an unconfigured ledger and then manufacture an image pin.
    if (
        not ledger_path.is_file()
        or ledger_path.is_symlink()
        or ledger_path.resolve(strict=True) != ledger_path
    ):
        raise ValueError("existing operator ledger required")
    ledger_info = ledger_path.stat()
    if ledger_info.st_uid != os.geteuid() or ledger_info.st_mode & 0o077:
        raise ValueError("operator ledger is not private")
    ledger = WorkspaceLeaseLedger(ledger_path, workspace_home=runtime.home)
    validate_runtime_pin(ledger, runtime, workspace)
    homes = AgentHomeRegistry(ledger, workspace_name=config["workspace_name"])
    agents = [UUID(value) for value in config["agent_ids"]]
    registered = [homes.lookup(workspace, agent) for agent in agents]
    if any(home is None for home in registered):
        raise ValueError("all probe agents must already be registered")
    monitor = WorkerMonitorConfig.from_environment()
    _outside_guest(monitor.audit_root, config)
    expected_probe = hashes["sandbox/qualification/worker_probe.py"]
    policy_pin = ExecutionPolicy(
        content_hash({"probe": "s46", "source": head}),
        content_hash(config),
        content_hash(hashes),
        __version__,
        max_wall_ms=20000,
        max_output_bytes=16384,
    )
    binding = WorkBinding("tool", OPERATION, INPUT, OUTPUT, expected_probe, "read_only", policy_pin)
    operation = SandboxedCogOperation(binding, (GUEST_EXECUTABLE,), expected_probe)
    mesh = SynWorkerNodeMesh()
    backend = SharedWorkspaceBubblewrapBackend(runtime)
    attempts = []

    class RecordedSupervisor(ProcessSupervisor):
        """Observation only: launch/monitor/termination remain in super().run."""

        def __init__(self):
            super().__init__(backend, monitor_config=monitor)
            self.results = []

        def run(self, request, **kwargs):
            result = super().run(request, **kwargs)
            self.results.append(result)
            return result

    for agent, home in zip(agents, registered, strict=True):
        run_id, nonce = uuid4(), secrets.token_hex(32)
        supervisor = RecordedSupervisor()
        executor = SandboxedCogExecutor(runtime, ledger, homes, supervisor, mesh)
        # This is explicitly a direct executor smoke, not a sealed SYN catalogue
        # invocation. Full execution provenance has its own PostgreSQL gate.
        context = CogRunContext(content_hash(hashes), uuid4(), uuid4(), run_id)
        value = await executor.execute(
            operation, TypedCogValue(INPUT, {"nonce": nonce}), context, actor, agent
        )
        if len(supervisor.results) != 1 or value.type_ref != OUTPUT:
            raise ValueError("one real bounded process/result required")
        check_process(supervisor.results[0])
        observed = check_probe(
            value.value,
            nonce=nonce,
            run_id=str(run_id),
            uid=home.uid,
            gid=home.gid,
            guest_cwd=home.guest_path,
        )
        # Reuse the existing full trace, process-tree, file-index and TASKSTATS
        # verifier; do not trust a guest assertion or lifetime.json boolean.
        verified = verify_gui_run(
            monitor.audit_root, workspace, run_id, identity_kind="agent", subject_id=agent
        )
        audit = check_audit(verified, workspace_id=str(workspace), run_id=str(run_id))
        validate_runtime_pin(ledger, runtime, workspace)
        attempts.append(
            {
                "agent_id": str(agent),
                "run_id": str(run_id),
                "qualified": True,
                "process_stopped": True,
                "observations": observed,
                "audit": audit,
                "registered_identity": {"uid": home.uid, "gid": home.gid},
                "isolation_sha256": digest(supervisor.results[0].isolation_identity.encode()),
            }
        )
    return report(
        source_commit=head,
        source_hashes=hashes,
        image_digest=runtime.image_digest,
        qualification_sha256=runtime.qualification_sha256,
        probe_sha256=expected_probe,
        workspace_id=str(workspace),
        agents=config["agent_ids"],
        attempts=attempts,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--run-live", action="store_true", help="Explicitly launch registered workers"
    )
    args = parser.parse_args(argv)
    phase = "opt_in"
    try:
        if not args.run_live:
            raise ValueError("explicit live opt-in required")
        if args.report.exists() or args.report.is_symlink():
            raise ValueError("report already exists; select a fresh output file")
        phase = "policy"
        config = policy(unique_json(read_private(args.policy)))
        for path in (args.policy, args.report):
            _outside_guest(path, config)
        # Validate output directory before any launch, without touching its ACL.
        parent = args.report.parent
        if (
            not args.report.is_absolute()
            or parent.resolve(strict=True) != parent
            or not parent.is_dir()
            or parent.stat().st_uid != os.geteuid()
            or parent.stat().st_mode & 0o077
        ):
            raise ValueError("private report directory required")
        phase = "committed_sources"
        head, hashes = committed_sources()
        phase = "live_execution_and_audit"
        result = asyncio.run(qualify(config, head, hashes))
        phase = "report_write"
        write_report(args.report, result)
    except Exception as exc:
        # Failed runs never overwrite or leave behind a success receipt. Detailed
        # operator traces remain in the existing audit root, not stdout.
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "qualified_smoke": False,
                    "phase": phase,
                    "error_class": type(exc).__name__,
                }
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "schema": SCHEMA,
                "qualified_smoke": True,
                "source_commit": head,
                "registered_workers": len(result["attempts"]),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
