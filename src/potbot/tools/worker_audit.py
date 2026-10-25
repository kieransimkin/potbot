"""Private, bounded postmortem summaries for operator-owned strace transcripts.

Never put this module behind a web route. The operator's raw trace and file
journal remain authoritative evidence; the summaries preserve limitations and
fail closed when a traced child lacks a corresponding trace. They do not turn
lexical paths into verified inode identities, or syscall wall time into CPU time.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from potbot.tools.taskstats_exit import ExitAccounting

from potbot.domain.errors import CapabilityUnavailableError, ScopeDeniedError

_LINE = re.compile(r"^\s*(?P<time>[0-9]{9,14}\.[0-9]+)\s+(?P<call>[a-zA-Z_][a-zA-Z_0-9]*)\(")
_RESUME = re.compile(r"^\s*(?P<time>[0-9]{9,14}\.[0-9]+)\s+<\.\.\.\s+(?P<call>[a-zA-Z_][a-zA-Z_0-9]*) resumed>(?P<rest>.*)$")
_CHILD_RETURN = re.compile(r"\)\s+=\s+([1-9][0-9]{0,9})(?:\s|$)")
_EXIT = re.compile(r"\+\+\+ exited with (?P<code>[0-9]+) \+\+\+")
_SIGNAL = re.compile(r"\+\+\+ killed by (?P<signal>SIG[A-Z0-9]+)")
_TRACE_NAME = re.compile(r"trace\.([1-9][0-9]{0,9})\Z")
_FORKS = frozenset(("fork", "vfork", "clone", "clone3"))
_MAX_PROCESSES = 32768
_MAX_INDEX_PATHS = 100000
_MAX_EVENT_LINE = 65536
_MAX_EVENT_ROWS = 2000000


def _checked_trace(path: Path) -> int:
    match = _TRACE_NAME.fullmatch(path.name)
    if not match or path.is_symlink():
        raise ScopeDeniedError("worker syscall transcript is not a regular, named trace")
    fileinfo = path.lstat()
    if not stat.S_ISREG(fileinfo.st_mode) or fileinfo.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ScopeDeniedError("worker syscall transcript has unsafe permissions")
    return int(match[1])


def _completed_calls(path: Path) -> Iterable[tuple[float, str, str]]:
    """Yield (entry timestamp, syscall, joined strace call), including resumed fork."""
    pending: tuple[str, str, float] | None = None
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            match, resume = _LINE.match(line), _RESUME.match(line)
            if match:
                name, stamp = match["call"], float(match["time"])
                text = line[match.start("call"):].rstrip("\n")
                if "<unfinished ...>" in text:
                    pending = (name, text.split("<unfinished ...>", 1)[0], stamp)
                    continue
                pending = None
                yield stamp, name, text
            elif resume:
                if pending and pending[0] == resume["call"]:
                    yield pending[2], pending[0], pending[1] + resume["rest"].rstrip("\n")
                pending = None


def process_tree(traces: Iterable[Path], *, sampled_processes: Mapping[str, Mapping[str, int]] | None = None,
                 syscall_lifetime: Mapping[str, Mapping[str, int]] | None = None,
                 kernel_exit: Mapping[int, ExitAccounting] | None = None) -> dict[str, Any]:
    """Account for observed tracees, including short-lived forked descendants.

    Linux PID may be reused; sampled entries retain start_ticks when present.
    Unobserved start_ticks are *not* fabricated. A trace-file PID by itself is
    only an observation scoped to this short worker lifetime.
    """
    paths = list(traces)
    if not 1 <= len(paths) <= _MAX_PROCESSES:
        raise CapabilityUnavailableError("tracee count is absent or beyond audit bound")
    pids: dict[int, Path] = {}
    for path in paths:
        pid = _checked_trace(path)
        if pid in pids:
            raise ScopeDeniedError("duplicate PID transcript in worker audit")
        pids[pid] = path
    children: dict[int, tuple[int, str]] = {}
    exit_codes: dict[int, dict[str, str | int]] = {}
    lifetime: dict[int, tuple[float, float]] = {}
    fork_conflict = 0
    for pid, path in sorted(pids.items()):
        first = last = None
        # Observe exit lines separately: they are not syscall invocations.
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                m = _LINE.match(line) or _RESUME.match(line)
                if m:
                    stamp = float(m["time"])
                    first = stamp if first is None else min(first, stamp)
                    last = stamp if last is None else max(last, stamp)
                if e := _EXIT.search(line):
                    exit_codes[pid] = {"state": "exited", "code": int(e["code"])}
                elif e := _SIGNAL.search(line):
                    exit_codes[pid] = {"state": "signalled", "signal": e["signal"]}
        for _, call, expression in _completed_calls(path):
            if call not in _FORKS: continue
            result = _CHILD_RETURN.search(expression)
            if not result: continue  # Failed fork or a child-side return.
            child = int(result[1])
            if child == pid: # Never accept an impossible self-edge.
                fork_conflict += 1
                continue
            relation = "thread" if call in {"clone", "clone3"} and "CLONE_THREAD" in expression else "process"
            expected = (pid, relation)
            if child in children and children[child] != expected:
                fork_conflict += 1
            else:
                children[child] = expected
        if first is not None and last is not None:
            lifetime[pid] = (first, last)
    missing = sorted(set(children) - set(pids))
    roots = sorted(set(pids) - set(children))
    # One normal root does not prove a tree: an unrelated two-node cycle can
    # coexist with it.  Detect all cycles in O(process_count) parent walks.
    visited: set[int] = set()
    cycle_pids: set[int] = set()
    for origin in pids:
        if origin in visited:
            continue
        local: dict[int, int] = {}
        chain: list[int] = []
        cursor = origin
        while cursor in pids and cursor not in visited:
            if cursor in local:
                cycle_pids.update(chain[local[cursor]:])
                break
            local[cursor] = len(chain)
            chain.append(cursor)
            cursor = children[cursor][0] if cursor in children else -1
        visited.update(chain)
    if kernel_exit is not None and set(kernel_exit) != set(pids):
        raise CapabilityUnavailableError("kernel taskstats does not cover every traced process")
    samples: dict[int, Mapping[str, int]] = {}
    for key, value in (sampled_processes or {}).items():
        try: pid_text, start_text = key.split(":", 1); pid = int(pid_text); start = int(start_text)
        except (ValueError, AttributeError): continue
        if pid in pids and type(value.get("start_ticks")) is int and value["start_ticks"] == start:
            samples[pid] = value
    nodes = []
    for pid in sorted(pids):
        sample = samples.get(pid)
        node: dict[str, Any] = {"pid": pid, "parent_pid": children[pid][0] if pid in children else None,
                                "kind": children[pid][1] if pid in children else "root",
                                "exit": exit_codes.get(pid, {"state": "unobserved"}),
                                "start_ticks": sample["start_ticks"] if sample else None,
                                "resource_fidelity": ("kernel_taskstats_exit" if kernel_exit is not None
                                     else "sampled_lower_bound" if sample else "not_observed")}
        if kernel_exit is not None:
            record = kernel_exit[pid]
            if record.pid != pid:
                raise CapabilityUnavailableError("kernel taskstats PID differs from trace identity")
            node["kernel_exit_resources"] = record.public_private_record()
        if pid in lifetime:
            node["first_syscall_at"], node["last_syscall_at"] = lifetime[pid]
        if sample:
            node["sampled_resources"] = {name: sample[name] for name in (
                "cpu_ticks_sampled", "rss_pages_sampled", "read_bytes_sampled", "write_bytes_sampled"
            ) if type(sample.get(name)) is int}
        if syscall_lifetime is not None:
            counters = syscall_lifetime.get(str(pid), {})
            if counters:
                node["syscall_lifetime"] = {name: value for name, value in counters.items()
                                            if type(value) is int and value >= 0}
        nodes.append(node)
    return {"schema": 1, "nodes": nodes, "roots": roots,
            "expected_but_untraced_pids": missing,
            "fork_conflicts": fork_conflict,
            "cycle_pids": sorted(cycle_pids),
            "tree_complete": len(roots) == 1 and not missing and fork_conflict == 0 and not cycle_pids,
            "pid_reuse_not_ruled_out_without_start_ticks": kernel_exit is None,
            "exit_resource_fidelity": ("kernel_taskstats_exit" if kernel_exit is not None
                                       else "sampled_lower_bound")}


def _private_new(path: Path) -> Any:
    if path.exists() or path.is_symlink():
        raise ScopeDeniedError("operator audit output already exists")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8", newline="\n")


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def file_access_index(event_file: Path, output_path: Path) -> dict[str, Any]:
    """Deduplicate the private file event stream with bounded memory.

    Both successful operations and failed access attempts are retained, each
    with its per-mode count. We preserve the strace-reported path literally:
    relative paths and symlink aliases are not claimed to be inode identities.
    """
    events: dict[tuple[int, str], dict[str, Any]] = {}
    row_count = 0
    fd = os.open(event_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        for line in stream:
            row_count += 1
            if row_count > _MAX_EVENT_ROWS or len(line) > _MAX_EVENT_LINE:
                raise CapabilityUnavailableError("worker file audit index budget exceeded")
            try:
                entry = json.loads(line)
            except ValueError as exc:
                raise CapabilityUnavailableError("worker audit contains invalid JSONL") from exc
            if not isinstance(entry, dict) or type(entry.get("pid")) is not int or not 0 < entry["pid"] < 2**31:
                raise CapabilityUnavailableError("worker file audit record has invalid PID")
            path, mode = entry.get("path"), entry.get("mode")
            if not isinstance(path, str) or not 0 < len(path) <= 8192 or mode not in {
                "read", "write", "read_write", "metadata_read", "metadata_write", "execute"
            }:
                raise CapabilityUnavailableError("worker file audit record has invalid path or mode")
            stamp = entry.get("time")
            if not isinstance(stamp, (float, int)) or type(stamp) is bool or not 0 < stamp < 1e12:
                raise CapabilityUnavailableError("worker file audit timestamp invalid")
            key = entry["pid"], path
            if key not in events:
                if len(events) >= _MAX_INDEX_PATHS:
                    raise CapabilityUnavailableError("worker file path cardinality exceeds audit ceiling")
                events[key] = {"first_at": stamp, "last_at": stamp, "modes": {}, "successful": 0,
                               "failed_attempts": 0, "path_is_lexical": True}
            item = events[key]
            item["first_at"] = min(item["first_at"], stamp)
            item["last_at"] = max(item["last_at"], stamp)
            outcome = "failed_attempts" if entry.get("result_error") is True else "successful"
            item[outcome] += 1
            by_mode = item["modes"].setdefault(mode, {"successful": 0, "failed_attempts": 0})
            by_mode[outcome] += 1
    digest = hashlib.sha256()
    with _private_new(output_path) as output:
        for (pid, path), data in sorted(events.items()):
            encoded = json.dumps({"pid": pid, "path": path, **data}, sort_keys=True, ensure_ascii=True,
                                 allow_nan=False, separators=(",", ":")) + "\n"
            output.write(encoded)
            digest.update(encoded.encode("utf-8"))
        output.flush()
        os.fsync(output.fileno())
    _fsync_directory(output_path.parent)
    return {"schema": 1, "file_keys": len(events), "events": row_count,
            "sha256": digest.hexdigest(), "lexical_paths_not_inode_identities": True}


def write_process_tree(tree: Mapping[str, Any], path: Path) -> dict[str, Any]:
    """Stream process observations privately, not through bounded live.json."""
    digest = hashlib.sha256()
    nodes = tree.get("nodes")
    if not isinstance(nodes, list) or len(nodes) > _MAX_PROCESSES:
        raise CapabilityUnavailableError("worker tree is not bounded")
    with _private_new(path) as output:
        for node in nodes:
            encoded = json.dumps(node, sort_keys=True, ensure_ascii=True,
                                 allow_nan=False, separators=(",", ":")) + "\n"
            output.write(encoded)
            digest.update(encoded.encode("utf-8"))
        output.flush()
        os.fsync(output.fileno())
    _fsync_directory(path.parent)
    return {"schema": 1, "nodes": len(nodes), "roots": tree["roots"],
            "expected_but_untraced_pids": tree["expected_but_untraced_pids"][:32],
            "expected_untraced_count": len(tree["expected_but_untraced_pids"]),
            "fork_conflicts": tree["fork_conflicts"], "tree_complete": tree["tree_complete"],
            "sha256": digest.hexdigest(), "file": "process-tree.jsonl"}
