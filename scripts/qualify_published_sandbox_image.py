"""Exercise an independently verified, unpacked image on a disposable CI host."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from potbot.evaluation.sandbox_worker_acceptance import write_report
from potbot.tools.ai_config_history import AiConfigHistoryMonitor
from potbot.tools.shared_workspace_isolation import SharedWorkspaceRuntime
from scripts.qualify_phase35_worker import qualify as qualify_worker
from scripts.qualify_shared_workspace import qualify as qualify_workspace
from scripts.qualify_worker_exit_accounting import qualify as qualify_accounting
from scripts.worker_audit_fixture import monitored_cog_cgroup

_SHIPPED_FILES = {
    "opt/potbot/context_client.py": "sandbox/clients/context_client.py",
    "usr/local/libexec/potbot-worker-probe": "sandbox/qualification/worker_probe.py",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rootfs", required=True, type=Path)
    parser.add_argument("--scratch", required=True, type=Path)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--image-source", required=True)
    parser.add_argument("--consumer", required=True)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    rootfs = args.rootfs.resolve(strict=True)
    scratch = args.scratch.resolve(strict=True)
    if rootfs.is_relative_to(scratch) or scratch.is_relative_to(rootfs):
        raise ValueError("scratch and unpacked image must be disjoint")
    if not args.report.is_absolute() or not args.report.parent.is_relative_to(scratch):
        raise ValueError("report must stay in the disposable qualification directory")

    source_root = Path(__file__).resolve().parents[1]
    shipped = {}
    for guest, source in _SHIPPED_FILES.items():
        candidate = rootfs / guest
        if candidate.resolve(strict=True) != candidate or not candidate.is_file():
            raise ValueError(f"unexpected installed path for {source}")
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if digest != hashlib.sha256((source_root / source).read_bytes()).hexdigest():
            raise ValueError(f"published image bytes differ for {source}")
        shipped[guest] = digest

    home, staging, models, tools, ai, history, private, audit = (
        scratch / name
        for name in ("home", "staging", "models", "tools", "ai", "history", "private", "audit")
    )
    for path in (home, staging, models, tools, ai, history, private, audit):
        path.mkdir(mode=0o700 if path in (private, audit) else 0o755)
    (home / ".tmp").mkdir()
    os.chown(tools, -1, 69)
    tools.chmod(0o2775)
    ai.chmod(0o775)
    runtime = SharedWorkspaceRuntime(
        args.image_digest,
        rootfs,
        home,
        "0" * 64,  # A candidate only. No image admission or workspace pin is changed.
        model_staging=staging,
        shared_models=models,
        shared_tools=tools,
        ai_config_dir=ai,
        ai_history_dir=history,
    )
    accounting = qualify_accounting()
    with (
        monitored_cog_cgroup(private, audit) as monitor,
        AiConfigHistoryMonitor(config_dir=ai, past_dir=history) as watcher,
    ):
        smoke = qualify_workspace(
            runtime, watcher, workspace_id=uuid4(), principal_id=uuid4(), monitor_config=monitor
        )
        worker = (
            qualify_worker(runtime, monitor, watcher=watcher)
            if smoke["qualified_smoke"] is True
            else None
        )

    passed = (
        accounting["qualified"] is True and worker is not None and worker["qualified_smoke"] is True
    )
    report = {
        "schema": "potbot.published-image-hosted-smoke/1",
        "scope": "disposable-linux-amd64",
        "qualified_smoke": passed,
        "image_digest": args.image_digest,
        "image_source_commit": args.image_source,
        "consumer_commit": args.consumer,
        "kernel_release": os.uname().release,
        "shipped_sha256": shipped,
        "taskstats": accounting,
        "workspace_smoke": smoke,
        "worker_fork_smoke": worker,
    }
    write_report(args.report, report)
    print(json.dumps({"qualified_smoke": passed, "image_digest": args.image_digest}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
