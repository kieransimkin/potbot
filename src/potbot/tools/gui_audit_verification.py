"""S16: host-private, read-only verification of S15 GUI kernel audit evidence.

Neither this module nor its CLI releases leases, kills processes or exposes
syscall transcript content, argv, file paths or provider credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any
from uuid import UUID

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_TRACE = re.compile(r"trace\.([1-9][0-9]{0,9})\Z")
_MODES = {"read", "write", "read_write", "metadata_read", "metadata_write", "execute"}
_MAX_WORKERS = 4096
_MAX_PROCESSES = 10000
_MAX_FILE_KEYS = 200000
_MAX_FILE_SIZE = 256 * 1024 * 1024


def _private_directory(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError("private operator audit directory unavailable")
    if path.resolve(strict=True) != path:
        raise ValueError("operator audit directory is redirected")
    info = path.stat(follow_symlinks=False)
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("operator audit directory permissions invalid")
    return path


def _read_private(path: Path, *, maximum: int = _MAX_FILE_SIZE) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1
                or not 0 <= info.st_size <= maximum):
            raise ValueError("private audit record invalid")
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(maximum + 1)
        if len(raw) != info.st_size or len(raw) > maximum:
            raise ValueError("private audit record size changed")
        after = os.fstat(fd)
        if (after.st_ino, after.st_dev, after.st_mtime_ns, after.st_size) != (
            info.st_ino, info.st_dev, info.st_mtime_ns, info.st_size
        ):
            raise ValueError("private audit record changed during inspection")
        return raw
    finally:
        os.close(fd)


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate audit JSON key")
        result[key] = value
    return result


def _json(data: bytes) -> Any:
    try:
        return json.loads(
            data, object_pairs_hook=_unique_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite audit number")),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("invalid private audit JSON") from exc


def _document(path: Path, maximum: int = 1048576) -> dict[str, Any]:
    value = _json(_read_private(path, maximum=maximum))
    if not isinstance(value, dict):
        raise ValueError("private audit record must be an object")
    return value


def _lines(path: Path, *, maximum: int = _MAX_FILE_SIZE,
           limit: int = _MAX_FILE_KEYS) -> tuple[list[dict[str, Any]], bytes]:
    raw = _read_private(path, maximum=maximum)
    if raw and not raw.endswith(b"\n"):
        raise ValueError("audit JSONL has a partial final line")
    lines = raw.splitlines()
    if len(lines) > limit:
        raise ValueError("audit index exceeds item allowance")
    records = []
    for line in lines:
        if not line or len(line) > 65536:
            raise ValueError("invalid audit JSONL record length")
        record = _json(line)
        if not isinstance(record, dict):
            raise ValueError("audit JSONL record must be an object")
        records.append(record)
    return records, raw


def _nonnegative(value: Any) -> int:
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError("invalid kernel accounting counter")
    return value


def _positive_pid(value: Any) -> int:
    if type(value) is not int or not 0 < value < 2**31:
        raise ValueError("invalid audited process identifier")
    return value


def _hash(data: bytes, expected: Any) -> None:
    if not isinstance(expected, str) or not _HEX.fullmatch(expected):
        raise ValueError("missing audit digest")
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("audit evidence digest mismatch")


def _canonical_uuid(value: Any) -> UUID:
    if not isinstance(value, str):
        raise ValueError("missing canonical audit UUID")
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError("noncanonical audit UUID")
    return parsed


def _verify_child(child: Path, workspace_id: UUID, run_id: UUID, *,
                  identity_kind: str, subject_id: UUID | None) -> dict[str, Any]:
    identity = _document(child / "identity.json", 65536)
    receipt = _document(child / "lifetime.json")
    if (identity.get("workspace_id") != str(workspace_id)
            or identity.get("worker_id") != child.name
            or identity.get("run_id") != str(run_id)
            or identity.get("identity_kind") != identity_kind
            or identity_kind not in {"principal", "agent"}):
        raise ValueError("GUI audit identity differs from expected scope")
    actual_subject = _canonical_uuid(identity.get("agent_id"))
    if subject_id is not None and actual_subject != subject_id:
        raise ValueError("GUI audit subject identity mismatch")
    if identity_kind == "agent" and subject_id is None:
        raise ValueError("agent audit requires an independently selected agent UUID")
    for key in ("workspace_id", "worker_id", "run_id", "agent_id", "identity_kind"):
        if receipt.get(key) != identity.get(key):
            raise ValueError("GUI lifetime receipt changed identity")
    integrity = receipt.get("trace_integrity")
    if (receipt.get("audit_complete") is not True
            or receipt.get("audit_failure") is not None
            or not isinstance(integrity, dict)
            or any(_nonnegative(v) != 0 for v in integrity.values())
            or receipt.get("per_pid_kernel_exit_accounting") is not True
            or receipt.get("taskstats_missing_exit_events") != 0):
        raise ValueError("GUI kernel audit is incomplete")
    tree_index, file_index = receipt.get("process_tree"), receipt.get("file_index")
    if not isinstance(tree_index, dict) or not isinstance(file_index, dict):
        raise ValueError("GUI audit indexes missing")
    nodes, tree_raw = _lines(child / "process-tree.jsonl", limit=_MAX_PROCESSES)
    files, file_raw = _lines(child / "file-index.jsonl")
    _hash(tree_raw, tree_index.get("sha256"))
    _hash(file_raw, file_index.get("sha256"))
    if (tree_index.get("tree_complete") is not True
            or tree_index.get("nodes") != len(nodes)
            or not 1 <= len(nodes) <= _MAX_PROCESSES
            or tree_index.get("expected_untraced_count") != 0
            or tree_index.get("fork_conflicts") != 0
            or file_index.get("file_keys") != len(files)):
        raise ValueError("GUI process or file index declarations differ")
    processes: dict[int, int | None] = {}
    for node in nodes:
        pid = _positive_pid(node.get("pid"))
        parent = node.get("parent_pid")
        if parent is not None:
            parent = _positive_pid(parent)
        if pid in processes or parent == pid:
            raise ValueError("duplicate or cyclic process identity")
        if node.get("resource_fidelity") != "kernel_taskstats_exit":
            raise ValueError("process lacks kernel task-exit accounting")
        kernel = node.get("kernel_exit_resources")
        if not isinstance(kernel, dict) or kernel.get("pid") != pid:
            raise ValueError("kernel exit process does not match trace")
        if not isinstance(node.get("exit"), dict) or node["exit"].get("state") not in {"exited", "signalled"}:
            raise ValueError("process has no observed exit")
        processes[pid] = parent
    roots = sorted(pid for pid, parent in processes.items() if parent is None)
    if len(roots) != 1 or tree_index.get("roots") != roots:
        raise ValueError("GUI process tree root differs")
    for pid in processes:
        visited = set()
        cursor: int | None = pid
        while cursor is not None:
            if cursor in visited or cursor not in processes:
                raise ValueError("process tree has a cycle or missing parent")
            visited.add(cursor)
            cursor = processes[cursor]
    if (receipt.get("traced_pid_count") != len(processes)
            or receipt.get("per_pid_kernel_exit_count") != len(processes)):
        raise ValueError("kernel task-exit coverage mismatch")
    trace_pids: set[int] = set()
    trace_hash = hashlib.sha256()
    traces = sorted((p for p in child.iterdir() if p.name.startswith("trace.")), key=lambda p: p.name)
    for trace in traces:
        match = _TRACE.fullmatch(trace.name)
        if match is None:
            raise ValueError("unexpected GUI syscall transcript")
        pid = int(match.group(1))
        if pid in trace_pids:
            raise ValueError("duplicate GUI trace PID")
        trace_pids.add(pid)
        trace_hash.update(_read_private(trace, maximum=_MAX_FILE_SIZE))
    if trace_pids != set(processes):
        raise ValueError("GUI trace PIDs differ from kernel exit accounting")
    expected_trace = receipt.get("trace_sha256")
    if not isinstance(expected_trace, str) or not _HEX.fullmatch(expected_trace) or trace_hash.hexdigest() != expected_trace:
        raise ValueError("GUI raw syscall trace hash mismatch")
    total_events = 0
    seen_files: set[tuple[int, str]] = set()
    for item in files:
        pid = _positive_pid(item.get("pid"))
        path = item.get("path")
        if (pid not in processes or not isinstance(path, str)
                or not 0 < len(path) <= 8192 or (pid, path) in seen_files
                or item.get("path_is_lexical") is not True):
            raise ValueError("GUI file access belongs to unknown process or path")
        seen_files.add((pid, path))
        successful = _nonnegative(item.get("successful"))
        failed = _nonnegative(item.get("failed_attempts"))
        modes = item.get("modes")
        if not isinstance(modes, dict) or not modes or set(modes) - _MODES:
            raise ValueError("GUI file access modes invalid")
        s_total = f_total = 0
        for counts in modes.values():
            if not isinstance(counts, dict) or set(counts) != {"successful", "failed_attempts"}:
                raise ValueError("GUI access mode counters invalid")
            s_total += _nonnegative(counts["successful"])
            f_total += _nonnegative(counts["failed_attempts"])
        if successful != s_total or failed != f_total or not successful + failed:
            raise ValueError("GUI file access totals inconsistent")
        first, last = item.get("first_at"), item.get("last_at")
        if (type(first) not in (int, float) or type(last) not in (int, float)
                or not 0 < first <= last < 1e12):
            raise ValueError("GUI file access times invalid")
        total_events += successful + failed
    if (file_index.get("events") != total_events
            or receipt.get("file_event_count") != total_events):
        raise ValueError("GUI file event count differs from actual index")
    usage = receipt.get("cgroup_usage")
    if not isinstance(usage, dict):
        raise ValueError("GUI cgroup resource totals missing")
    for field in ("cpu_usec", "read_bytes", "write_bytes", "memory_peak_bytes"):
        _nonnegative(usage.get(field))
    return {
        "schema": "potbot-gui-audit-integrity-s16/v1",
        "workspaceId": str(workspace_id),
        "runId": str(run_id),
        "identityKind": identity_kind,
        "qualified": True,
        "tracedProcesses": len(nodes),
        "fileKeys": len(files),
        "fileEvents": total_events,
        "cpuUsec": usage["cpu_usec"],
        "readBytes": usage["read_bytes"],
        "writeBytes": usage["write_bytes"],
        "peakMemoryBytes": usage["memory_peak_bytes"],
    }


def verify_gui_run(audit_root: Path, workspace_id: UUID, run_id: UUID, *,
                   identity_kind: str = "principal", subject_id: UUID | None = None) -> dict[str, Any]:
    if not isinstance(workspace_id, UUID) or not isinstance(run_id, UUID):
        raise ValueError("GUI audit needs canonical workspace and run UUIDs")
    if identity_kind not in {"principal", "agent"}:
        raise ValueError("invalid GUI audit subject type")
    root = _private_directory(Path(audit_root))
    workspace = _private_directory(root / str(workspace_id))
    matches = []
    count = 0
    for child in workspace.iterdir():
        if child.name == ".active":
            continue
        count += 1
        if count > _MAX_WORKERS:
            raise ValueError("too many GUI audit worker records")
        _canonical_uuid(child.name)
        _private_directory(child)
        identity = _document(child / "identity.json", 65536)
        if identity.get("run_id") == str(run_id):
            matches.append(child)
            if len(matches) > 1:
                raise ValueError("ambiguous GUI audit run")
    if len(matches) != 1:
        raise ValueError("GUI audit run unavailable")
    return _verify_child(matches[0], workspace_id, run_id,
                         identity_kind=identity_kind, subject_id=subject_id)


def inspect_recovery(audit_root: Path, workspace_id: UUID, *, limit: int = 512) -> dict[str, Any]:
    """Read-only restart inventory; never releases leases or deletes cgroups."""
    if not isinstance(workspace_id, UUID) or type(limit) is not int or not 1 <= limit <= 512:
        raise ValueError("invalid GUI recovery bounds")
    workspace = _private_directory(_private_directory(Path(audit_root)) / str(workspace_id))
    active_directory = workspace / ".active"
    active_names = set()
    if active_directory.exists():
        _private_directory(active_directory)
        for marker in active_directory.iterdir():
            if not marker.name.endswith(".json"):
                raise ValueError("unexpected active monitor marker")
            _canonical_uuid(marker.name[:-5])
            _document(marker, 65536)
            active_names.add(marker.name[:-5])
    entries = []
    counts: dict[str, int] = {}
    for child in sorted(workspace.iterdir(), key=lambda path: path.name):
        if child.name == ".active":
            continue
        if len(entries) >= limit:
            raise ValueError("GUI recovery inventory exceeds item limit")
        _canonical_uuid(child.name)
        _private_directory(child)
        try:
            identity = _document(child / "identity.json", 65536)
            run = _canonical_uuid(identity.get("run_id"))
            if identity.get("workspace_id") != str(workspace_id) or identity.get("worker_id") != child.name:
                raise ValueError("GUI recovery identity differs")
            kind = identity.get("identity_kind")
            if kind not in {"principal", "agent"}:
                raise ValueError("unknown GUI audit kind")
            if child.name in active_names:
                status = "unreconciled_active"
            elif not (child / "lifetime.json").exists():
                status = "missing_receipt"
            else:
                receipt = _document(child / "lifetime.json")
                if receipt.get("audit_complete") is not True:
                    status = "incomplete"
                else:
                    try:
                        _verify_child(child, workspace_id, run, identity_kind=kind,
                                      subject_id=_canonical_uuid(identity.get("agent_id")))
                        status = "qualified"
                    except (ValueError, OSError, KeyError, TypeError):
                        status = "invalid_evidence"
            entries.append({"workerId": child.name, "runId": str(run), "identityKind": kind,
                            "status": status})
        except (ValueError, OSError, TypeError, KeyError):
            status = "invalid_identity"
            entries.append({"workerId": child.name, "runId": None,
                            "identityKind": None, "status": status})
        counts[status] = counts.get(status, 0) + 1
    unknown = active_names - {entry["workerId"] for entry in entries}
    if unknown:
        raise ValueError("orphaned GUI monitor active markers")
    return {"schema": "potbot-gui-recovery-s16/v1", "workspaceId": str(workspace_id),
            "total": len(entries), "counts": counts, "runs": entries,
            "requiresReconciliation": any(entry["status"] != "qualified" for entry in entries)}
