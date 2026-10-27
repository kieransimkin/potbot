"""Offline end-to-end acceptance of the private GUI kernel-audit receipt."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from scripts.qualify_gui_telemetry_s15 import qualify


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def fixture(tmp_path: Path):
    root = tmp_path / "audit"
    workspace_id, run_id, principal = uuid4(), uuid4(), uuid4()
    session = root / str(workspace_id) / str(uuid4())
    session.mkdir(parents=True)
    root.chmod(0o700)
    session.parent.chmod(0o700)
    session.chmod(0o700)
    for name in ("process-tree.jsonl", "file-index.jsonl"):
        (session / name).write_bytes(b"{}\n")
        (session / name).chmod(0o600)
    digest = hashlib.sha256(b"{}\n").hexdigest()
    identity = {"workspace_id": str(workspace_id), "agent_id": str(principal),
                "worker_id": session.name, "run_id": str(run_id),
                "identity_kind": "principal"}
    receipt = {**identity, "audit_complete": True, "audit_failure": None,
               "trace_integrity": {}, "taskstats_missing_exit_events": 0,
               "per_pid_kernel_exit_accounting": True,
               "traced_pid_count": 3, "per_pid_kernel_exit_count": 3,
               "process_tree": {"sha256": digest, "nodes": 3, "tree_complete": True,
                                "expected_untraced_count": 0, "fork_conflicts": 0},
               "file_index": {"sha256": digest, "file_keys": 8},
               "cgroup_usage": {"cpu_usec": 123, "read_bytes": 1000,
                                "write_bytes": 9, "memory_peak_bytes": 65536}}
    write_json(session / "identity.json", identity)
    write_json(session / "lifetime.json", receipt)
    return root, workspace_id, run_id, session, receipt


def test_valid_gui_audit_qualifies_without_exposing_private_paths(tmp_path):
    root, ws, run, session, receipt = fixture(tmp_path)
    result = qualify(root, ws, run)
    assert result["qualified"] is True
    assert result["fileKeys"] == 8
    assert "path" not in json.dumps(result).lower()
    assert str(root) not in json.dumps(result)


def test_missing_descendant_or_exit_event_fails(tmp_path):
    root, ws, run, session, receipt = fixture(tmp_path)
    for field, value in (("per_pid_kernel_exit_count", 2),
                         ("taskstats_missing_exit_events", 1),
                         ("audit_complete", False)):
        broken = {**receipt, field: value}
        write_json(session / "lifetime.json", broken)
        with pytest.raises(ValueError):
            qualify(root, ws, run)


def test_tampered_file_access_index_fails(tmp_path):
    root, ws, run, session, receipt = fixture(tmp_path)
    with (session / "file-index.jsonl").open("ab") as f:
        f.write(b"secret path\n")
    with pytest.raises(ValueError):
        qualify(root, ws, run)


def test_untrusted_symlink_directory_fails(tmp_path):
    root, ws, run, session, receipt = fixture(tmp_path)
    alias = session.parent / str(uuid4())
    alias.symlink_to(session, target_is_directory=True)
    with pytest.raises(ValueError):
        qualify(root, ws, run)
