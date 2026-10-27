"""S16: real private-file fixtures; no kernel or browser process required."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.tools.gui_audit_verification import inspect_recovery, verify_gui_run


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    path.chmod(0o600)


def fixture(tmp_path: Path, *, kind: str = "principal") -> tuple[Path, object, object, object, Path]:
    root, workspace_id, run_id, subject = tmp_path / "audit", uuid4(), uuid4(), uuid4()
    child = root / str(workspace_id) / str(uuid4())
    child.mkdir(parents=True)
    for directory in (root, child.parent, child):
        directory.chmod(0o700)
    pid = 101
    process = {"pid": pid, "parent_pid": None, "kind": "root",
               "resource_fidelity": "kernel_taskstats_exit",
               "kernel_exit_resources": {"pid": pid, "cpu_usec": 12},
               "exit": {"state": "exited", "code": 0}}
    file_record = {"pid": pid, "path": "/home/private/test-file.txt",
                   "path_is_lexical": True, "first_at": 1.2, "last_at": 1.3,
                   "successful": 2, "failed_attempts": 1,
                   "modes": {"read": {"successful": 2, "failed_attempts": 0},
                             "write": {"successful": 0, "failed_attempts": 1}}}
    for name, content in (("process-tree.jsonl", process), ("file-index.jsonl", file_record)):
        _write(child / name, content)
    trace_data = b"169.1 execve(\"/bin/true\", [...]) = 0\n+++ exited with 0 +++\n"
    (child / f"trace.{pid}").write_bytes(trace_data)
    (child / f"trace.{pid}").chmod(0o600)
    digest = lambda name: hashlib.sha256((child / name).read_bytes()).hexdigest()
    identity = {"schema": 1, "workspace_id": str(workspace_id), "run_id": str(run_id),
                "agent_id": str(subject), "worker_id": child.name,
                "identity_kind": kind, "image_identity": "a" * 64}
    usage = {"cpu_usec": 9, "read_bytes": 7, "write_bytes": 3, "memory_peak_bytes": 4096}
    receipt = {"schema": 1, **identity, "audit_complete": True, "audit_failure": None,
               "trace_integrity": {"unpaired_syscalls": 0},
               "per_pid_kernel_exit_accounting": True, "taskstats_missing_exit_events": 0,
               "process_tree": {"sha256": digest("process-tree.jsonl"),
                                "nodes": 1, "roots": [pid], "tree_complete": True,
                                "expected_untraced_count": 0, "fork_conflicts": 0},
               "file_index": {"sha256": digest("file-index.jsonl"), "events": 3,
                              "file_keys": 1},
               "traced_pid_count": 1, "per_pid_kernel_exit_count": 1,
               "trace_sha256": digest(f"trace.{pid}"), "file_event_count": 3,
               "cgroup_usage": usage}
    _write(child / "identity.json", identity)
    _write(child / "lifetime.json", receipt)
    return root, workspace_id, run_id, subject, child


def verify(fix, **kwargs):
    root, ws, run, _, _ = fix
    return verify_gui_run(root, ws, run, **kwargs)


def mutate_receipt(fix, key, value):
    child = fix[-1]
    record = json.loads((child / "lifetime.json").read_text())
    record[key] = value
    _write(child / "lifetime.json", record)


def test_valid_principal_evidence_is_qualified_without_paths(tmp_path):
    fix = fixture(tmp_path)
    result = verify(fix, subject_id=fix[3])
    assert result["qualified"] and result["tracedProcesses"] == 1
    assert result["fileEvents"] == 3 and result["fileKeys"] == 1
    assert "/home/private" not in json.dumps(result)
    assert str(fix[-1]) not in json.dumps(result)


def test_valid_agent_evidence_requires_explicit_agent_uuid(tmp_path):
    fix = fixture(tmp_path, kind="agent")
    with pytest.raises(ValueError): verify(fix, identity_kind="agent")
    assert verify(fix, identity_kind="agent", subject_id=fix[3])["identityKind"] == "agent"
    with pytest.raises(ValueError): verify(fix, identity_kind="agent", subject_id=uuid4())
    with pytest.raises(ValueError): verify(fix)


@pytest.mark.parametrize("field,value", [
    ("audit_complete", False),
    ("audit_failure", "monitor_lost"),
    ("taskstats_missing_exit_events", 1),
    ("per_pid_kernel_exit_accounting", False),
    ("trace_integrity", {"unpaired_syscalls": 1}),
    ("traced_pid_count", 2),
    ("per_pid_kernel_exit_count", 0),
    ("file_event_count", 2),
    ("trace_sha256", "0" * 64),
])
def test_failure_or_missing_kernel_accounting_rejected(tmp_path, field, value):
    fix = fixture(tmp_path)
    mutate_receipt(fix, field, value)
    with pytest.raises(ValueError): verify(fix)


def test_zero_valued_integrity_counters_are_legitimate(tmp_path):
    fix = fixture(tmp_path)
    mutate_receipt(fix, "trace_integrity", {"unpaired_syscalls": 0, "invalid_trace_filenames": 0})
    assert verify(fix)["qualified"]


def test_tampering_tree_or_file_index_rejected(tmp_path):
    for filename in ("process-tree.jsonl", "file-index.jsonl"):
        fix = fixture(tmp_path / filename.removesuffix(".jsonl"))
        with (fix[-1] / filename).open("ab") as output:
            output.write(b"private-tampering\n")
        with pytest.raises(ValueError): verify(fix)


def test_tampering_raw_trace_is_rejected(tmp_path):
    fix = fixture(tmp_path)
    (fix[-1] / "trace.101").write_bytes(b"modified")
    with pytest.raises(ValueError): verify(fix)


def test_file_index_semantics_rechecked_even_if_hash_repaired(tmp_path):
    fix = fixture(tmp_path)
    child = fix[-1]
    file_path = child / "file-index.jsonl"
    item = json.loads(file_path.read_text())
    item["modes"]["read"]["successful"] = 5
    _write(file_path, item)
    lifetime = json.loads((child / "lifetime.json").read_text())
    lifetime["file_index"]["sha256"] = hashlib.sha256(file_path.read_bytes()).hexdigest()
    _write(child / "lifetime.json", lifetime)
    with pytest.raises(ValueError): verify(fix)


def test_process_tree_semantics_rechecked_even_if_hash_repaired(tmp_path):
    fix = fixture(tmp_path)
    child = fix[-1]
    tree_path = child / "process-tree.jsonl"
    item = json.loads(tree_path.read_text())
    item["parent_pid"] = 999
    _write(tree_path, item)
    lifetime = json.loads((child / "lifetime.json").read_text())
    lifetime["process_tree"]["sha256"] = hashlib.sha256(tree_path.read_bytes()).hexdigest()
    _write(child / "lifetime.json", lifetime)
    with pytest.raises(ValueError): verify(fix)


def test_missing_or_unknown_trace_process_rejected(tmp_path):
    fix = fixture(tmp_path)
    (fix[-1] / "trace.101").rename(fix[-1] / "trace.999")
    with pytest.raises(ValueError): verify(fix)


def test_duplicate_json_key_rejected(tmp_path):
    fix = fixture(tmp_path)
    (fix[-1] / "identity.json").write_text('{"run_id":"x","run_id":"y"}\n')
    with pytest.raises(ValueError): verify(fix)


def test_untrusted_symlink_rejected(tmp_path):
    fix = fixture(tmp_path)
    path = fix[-1] / "trace.101"
    path.unlink()
    path.symlink_to("/etc/hosts")
    with pytest.raises((ValueError, OSError)): verify(fix)


def test_recovery_inventory_distinguishes_qualified_and_missing_receipt(tmp_path):
    fix = fixture(tmp_path)
    assert inspect_recovery(fix[0], fix[1])["counts"] == {"qualified": 1}
    (fix[-1] / "lifetime.json").unlink()
    report = inspect_recovery(fix[0], fix[1])
    assert report["requiresReconciliation"]
    assert report["counts"] == {"missing_receipt": 1}
    assert "/home/private" not in json.dumps(report)


def test_recovery_inventory_detects_stale_active_marker(tmp_path):
    fix = fixture(tmp_path)
    active = fix[-1].parent / ".active"
    active.mkdir(mode=0o700)
    active.chmod(0o700)
    _write(active / (fix[-1].name + ".json"), {"schema": 1, "state": "running"})
    assert inspect_recovery(fix[0], fix[1])["counts"] == {"unreconciled_active": 1}


def test_recovery_inventory_refuses_orphan_marker(tmp_path):
    fix = fixture(tmp_path)
    active = fix[-1].parent / ".active"
    active.mkdir(mode=0o700)
    _write(active / (str(uuid4()) + ".json"), {"schema": 1, "state": "running"})
    with pytest.raises(ValueError): inspect_recovery(fix[0], fix[1])


def test_read_only_recovery_never_mutates_evidence(tmp_path):
    fix = fixture(tmp_path)
    before = {p.name: p.read_bytes() for p in fix[-1].iterdir() if p.is_file()}
    inspect_recovery(fix[0], fix[1])
    assert {p.name: p.read_bytes() for p in fix[-1].iterdir() if p.is_file()} == before
