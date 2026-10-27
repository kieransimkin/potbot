#!/usr/bin/env python3
"""Read-only host acceptance for one completed S15 GUI cgroup/syscall audit.

No secrets, raw paths, trace contents, or argv leave the private operator store.
The run ID of an Xpra application equals its API session ID. Run this only on
an operator host after that process has stopped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from uuid import UUID

_HEX = re.compile(r"[0-9a-f]{64}\Z")


def private_dir(path: Path) -> Path:
    if (not path.is_absolute() or path.is_symlink() or not path.is_dir()
            or path.resolve(strict=True) != path or path.stat().st_uid != os.getuid()
            or path.stat().st_mode & 0o077):
        raise ValueError("private operator directory unavailable")
    return path


def record(path: Path, limit: int = 1024 * 1024) -> dict:
    if (path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077
            or path.stat().st_size > limit):
        raise ValueError("private audit record invalid")
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict):
        raise ValueError("invalid audit JSON record")
    return data


def verified_hash(path: Path, expected: str) -> None:
    if (not isinstance(expected, str) or not _HEX.fullmatch(expected)
            or path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077
            or path.stat().st_size > 256 * 1024 * 1024):
        raise ValueError("private audit index invalid")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ValueError("GUI audit index hash mismatch")


def qualify(audit_root: Path, workspace_id: UUID, run_id: UUID) -> dict[str, object]:
    workspace = private_dir(private_dir(audit_root) / str(workspace_id))
    candidates = []
    for child in workspace.iterdir():
        if child.name.startswith("."):
            continue
        private_dir(child)
        try:
            if str(UUID(child.name)) != child.name:
                raise ValueError
        except ValueError:
            raise ValueError("unexpected worker audit directory") from None
        identity = record(child / "identity.json", 65536)
        if identity.get("run_id") == str(run_id):
            candidates.append((child, identity))
        if len(candidates) > 1:
            raise ValueError("ambiguous GUI audit identity")
    if len(candidates) != 1:
        raise ValueError("GUI run not present")
    child, identity = candidates[0]
    receipt = record(child / "lifetime.json")
    if (identity.get("workspace_id") != str(workspace_id)
            or identity.get("identity_kind") != "principal"
            or receipt.get("agent_id") != identity.get("agent_id")
            or receipt.get("worker_id") != identity.get("worker_id")
            or receipt.get("run_id") != str(run_id)
            or receipt.get("identity_kind") != "principal"
            or receipt.get("audit_complete") is not True
            or receipt.get("audit_failure") is not None
            or receipt.get("trace_integrity") != {}
            or receipt.get("taskstats_missing_exit_events") != 0
            or receipt.get("per_pid_kernel_exit_accounting") is not True):
        raise ValueError("kernel accounting not complete")
    tree, files = receipt.get("process_tree"), receipt.get("file_index")
    if (not isinstance(tree, dict) or not isinstance(files, dict)
            or tree.get("tree_complete") is not True
            or tree.get("expected_untraced_count") != 0
            or tree.get("fork_conflicts") != 0
            or not isinstance(tree.get("nodes"), int) or tree["nodes"] < 1
            or receipt.get("traced_pid_count") != receipt.get("per_pid_kernel_exit_count")):
        raise ValueError("descendant coverage incomplete")
    verified_hash(child / "process-tree.jsonl", tree.get("sha256"))
    verified_hash(child / "file-index.jsonl", files.get("sha256"))
    usage = receipt.get("cgroup_usage")
    keys = ("cpu_usec", "read_bytes", "write_bytes", "memory_peak_bytes")
    if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in keys):
        raise ValueError("kernel resource totals unavailable")
    return {"schema": "potbot-gui-kernel-audit-s15/v1", "workspaceId": str(workspace_id),
            "runId": str(run_id), "qualified": True, "tracedProcesses": tree["nodes"],
            "fileKeys": files["file_keys"], "cpuUsec": usage["cpu_usec"],
            "readBytes": usage["read_bytes"], "writeBytes": usage["write_bytes"],
            "peakMemoryBytes": usage["memory_peak_bytes"]}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("audit-root", "workspace-id", "run-id", "report"):
        p.add_argument("--" + name, required=True)
    a = p.parse_args()
    try:
        result = qualify(Path(a.audit_root), UUID(a.workspace_id), UUID(a.run_id))
        path = Path(a.report)
        if not path.is_absolute() or path.exists() or path.is_symlink():
            raise ValueError("report must be new and absolute")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(result, f, sort_keys=True)
            f.write("\n"); f.flush(); os.fsync(f.fileno())
        print(json.dumps({"qualified": True, "reportSha256": hashlib.sha256(path.read_bytes()).hexdigest()}))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print('{"qualified":false,"error":"gui_audit_unavailable"}', file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
