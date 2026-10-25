"""Process-tree provenance and private per-file lifetime aggregation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from potbot.domain.errors import CapabilityUnavailableError, ScopeDeniedError
from potbot.tools.worker_audit import file_access_index, process_tree, write_process_tree


def test_fork_tree_captures_short_lived_process_and_threads(tmp_path: Path) -> None:
    parent = tmp_path / "trace.300"
    parent.write_text('''1793000000.000010 clone(child_stack=NULL, flags=CLONE_VM|CLONE_THREAD, child_tidptr=0x42) = 301 <0.000010>
1793000000.000020 clone3({flags=0}, 88 <unfinished ...>
1793000000.000030 <... clone3 resumed>) = 302 <0.000010>
1793000000.000100 +++ exited with 0 +++
''')
    (tmp_path / "trace.301").write_text('1793000000.000011 execve("/usr/bin/true", ["true"], 0x1) = 0 <0.001>\n')
    (tmp_path / "trace.302").write_text('1793000000.000040 +++ killed by SIGKILL +++\n')
    paths = sorted(tmp_path.glob("trace.*"))
    tree = process_tree(paths, sampled_processes={"301:12345": {"start_ticks": 12345}})
    assert tree["tree_complete"] is True
    assert tree["roots"] == [300]
    nodes = {item["pid"]: item for item in tree["nodes"]}
    assert nodes[301]["kind"] == "thread" and nodes[301]["parent_pid"] == 300
    assert nodes[301]["start_ticks"] == 12345
    assert nodes[302]["kind"] == "process"
    assert nodes[302]["exit"] == {"state": "signalled", "signal": "SIGKILL"}
    assert nodes[300]["exit"] == {"state": "exited", "code": 0}
    output = tmp_path / "process-tree.jsonl"
    summary = write_process_tree(tree, output)
    assert summary["nodes"] == 3 and summary["tree_complete"] is True
    assert hashlib.sha256(output.read_bytes()).hexdigest() == summary["sha256"]
    assert output.stat().st_mode & 0o077 == 0


def test_fork_child_without_trace_fails_closed(tmp_path: Path) -> None:
    parent = tmp_path / "trace.12"
    parent.write_text('1793000000.000010 fork() = 13 <0.000010>\n')
    tree = process_tree([parent])
    assert tree["tree_complete"] is False
    assert tree["expected_but_untraced_pids"] == [13]
    assert tree["roots"] == [12]


def test_trace_symlink_and_duplicate_are_denied(tmp_path: Path) -> None:
    real = tmp_path / "trace.18"
    real.write_text('1793000000.000010 getpid() = 18 <0.000001>\n')
    link = tmp_path / "trace.19"
    link.symlink_to(real)
    with pytest.raises(ScopeDeniedError):
        process_tree([real, link])
    with pytest.raises(ScopeDeniedError):
        process_tree([real, real])


def test_private_file_index_preserves_modes_and_failure_counts(tmp_path: Path) -> None:
    path = tmp_path / "file-events.jsonl"
    records = [
        {"pid": 900, "path": "somefile", "mode": "read", "time": 1793000000.01, "result_error": True},
        {"pid": 900, "path": "somefile", "mode": "read", "time": 1793000000.02, "result_error": False},
        {"pid": 900, "path": "somefile", "mode": "write", "time": 1793000000.03, "result_error": False},
        {"pid": 901, "path": "/home/a", "mode": "execute", "time": 1793000000.01, "result_error": False},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    output = tmp_path / "file-index.jsonl"
    digest = file_access_index(path, output)
    entries = [json.loads(line) for line in output.read_text().splitlines()]
    assert digest["events"] == 4 and digest["file_keys"] == 2
    assert digest["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert entries[0]["pid"] == 900 and entries[0]["path"] == "somefile"
    assert entries[0]["successful"] == 2 and entries[0]["failed_attempts"] == 1
    assert entries[0]["modes"]["read"] == {"successful": 1, "failed_attempts": 1}
    assert entries[0]["path_is_lexical"] is True
    assert output.stat().st_mode & 0o077 == 0
    with pytest.raises(ScopeDeniedError):
        file_access_index(path, output)


def test_corrupt_audit_is_not_summarised(tmp_path: Path) -> None:
    data = tmp_path / "events"
    data.write_text('{"pid":100,"path":"a","mode":"garbage","time":123.4}\n')
    with pytest.raises(CapabilityUnavailableError):
        file_access_index(data, tmp_path / "index")


def test_reader_suppresses_unverified_agent_attribution(tmp_path: Path) -> None:
    from uuid import uuid4
    import time
    from potbot.tools.worker_telemetry import _safe_write, OperatorTelemetryReader

    workspace = uuid4()
    root = tmp_path / "audit"
    root.mkdir(mode=0o700)
    known, unknown = uuid4(), uuid4()
    for agent, kind in ((known, "agent"), (unknown, "principal")):
        folder = root / str(workspace) / str(uuid4())
        folder.mkdir(parents=True, mode=0o700)
        _safe_write(folder / "live.json", {"schema": 1, "state": "running",
            "workspace_id": str(workspace), "worker_id": folder.name, "agent_id": str(agent),
            "identity_kind": kind, "sampled_at": time.time(),
            "usage": {"cpu_usec": 100, "memory_current_bytes": 10, "pids_current": 1}})
    reader = OperatorTelemetryReader(root)
    try:
        view = reader.snapshot(workspace)
        assert {a["agentId"] for a in view["agents"]} == {str(known)}
        assert len(view["workers"]) == 2
        assert str(unknown) not in json.dumps(view)
        assert {w["identityKind"] for w in view["workers"]} == {"agent", "principal"}
    finally:
        reader.close()


def test_active_worker_index_skips_old_history_and_prunes_heat_counters(tmp_path: Path) -> None:
    from uuid import uuid4
    import time
    from potbot.tools.worker_telemetry import _safe_write, OperatorTelemetryReader

    workspace, agent, worker = uuid4(), uuid4(), uuid4()
    root = tmp_path / "audit"
    root.mkdir(mode=0o700)
    home = root / str(workspace)
    home.mkdir(mode=0o700)
    # More completed workers than the old unsorted 256-entry directory scan.
    for i in range(280):
        (home / f"completed_{i}").mkdir(mode=0o700)
    active = home / ".active"
    active.mkdir(mode=0o700)
    live_path = active / f"{worker}.json"
    _safe_write(live_path, {"schema": 1, "state": "running", "workspace_id": str(workspace),
                            "agent_id": str(agent), "worker_id": str(worker),
                            "identity_kind": "agent", "sampled_at": time.time(),
                            "usage": {"cpu_usec": 10, "pids_current": 1, "memory_current_bytes": 400}})
    reader = OperatorTelemetryReader(root)
    try:
        first = reader.snapshot(workspace)
        assert len(first["workers"]) == 1
        assert first["agents"][0]["agentId"] == str(agent)
        assert first["audit"]["workerIndexOverflow"] is False
        assert len(reader._previous_workers) == 1
        live_path.unlink()
        second = reader.snapshot(workspace)
        assert second["workers"] == [] and second["agents"] == []
        assert reader._previous_workers == {}
    finally:
        reader.close()


def test_index_overflow_never_reports_partial_agent_heat(tmp_path: Path) -> None:
    from uuid import uuid4
    import time
    from potbot.tools.worker_telemetry import _safe_write, OperatorTelemetryReader

    workspace, agent = uuid4(), uuid4()
    root = tmp_path / "audit"; root.mkdir(mode=0o700)
    active = root / str(workspace) / ".active"; active.mkdir(parents=True, mode=0o700)
    for i in range(1025):
        w = uuid4()
        _safe_write(active / f"{w}.json", {"schema": 1, "state": "running",
            "workspace_id": str(workspace), "agent_id": str(agent), "worker_id": str(w),
            "identity_kind": "agent", "sampled_at": time.time(),
            "usage": {"cpu_usec": 10, "pids_current": 1, "memory_current_bytes": 1}})
    reader = OperatorTelemetryReader(root)
    try:
        view = reader.snapshot(workspace)
        assert view["audit"]["workerIndexOverflow"] is True
        assert view["agents"] == []
    finally:
        reader.close()


def test_disconnected_clone_cycle_is_not_a_complete_process_tree(tmp_path):
    """A valid root and a disconnected cycle used to evade root-count validation."""
    from potbot.tools.worker_audit import process_tree
    (tmp_path / "trace.11").write_text(
        '1792130000.000001 execve("/bin/true", [], []) = 0 <0.00001>\n'
        '+++ exited with 0 +++\n'
    )
    (tmp_path / "trace.12").write_text(
        '1792130000.100000 clone(child_stack=NULL, flags=SIGCHLD) = 13 <0.00001>\n'
        '+++ exited with 0 +++\n'
    )
    (tmp_path / "trace.13").write_text(
        '1792130000.200000 clone(child_stack=NULL, flags=SIGCHLD) = 12 <0.00001>\n'
        '+++ exited with 0 +++\n'
    )
    tree = process_tree(sorted(tmp_path.glob('trace.*')))
    assert tree['roots'] == [11]
    assert tree['cycle_pids'] == [12, 13]
    assert tree['tree_complete'] is False
