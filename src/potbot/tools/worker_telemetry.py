"""Operator-owned Linux Cog worker audit and read-only utilisation projection.

The trace process is launched by the trusted supervisor *outside* Bubblewrap,
following the whole fork tree from its first syscall. Every descendant inherits
an operator-created cgroup-v2 leaf before exec. The sandbox has no cgroup or
operator audit mount. Raw traces can contain secrets; NEVER publish them in UI.

Strict accounting: cgroup CPU/IO lifetime totals and system load are kernel
counters; individual PID CPU/RSS/IO observations are sampled lower bounds.
The syscall/file event history itself is traced, not sampled. A monitor fault
fences successful completion. A pinned, single-purpose native helper joins the
operator-owned cgroup BEFORE executing strace (no unsafe Python preexec_fn).
Production exit accounting will require qualified taskstats or eBPF support.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from potbot.domain.errors import CapabilityUnavailableError, ScopeDeniedError

_TRACE_LINE = re.compile(r"^\s*(?P<stamp>\d+\.\d+)\s+(?P<call>[A-Za-z_][A-Za-z_0-9]*)\(")
_TRACE_RESUMED = re.compile(r"^\s*(?P<stamp>\d+\.\d+)\s+<\.\.\.\s+(?P<call>[A-Za-z_][A-Za-z_0-9]*) resumed>(?P<rest>.*)$")
_TRACE_END_TIME = re.compile(r"\s<(?P<seconds>\d+\.\d+)>\s*$")
# These modify both the source *and* the destination pathname.
_DUAL_PATH = frozenset({"rename", "renameat", "renameat2", "link", "linkat"})
_UNFINISHED = "<unfinished ...>"
_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
_FD_PATH = re.compile(r"\b\d+<(/[^>]{1,8192})>")
_PID = re.compile(r"[1-9][0-9]{0,9}\Z")
_MAX_TRACE = 256 * 1024 * 1024  # Per worker; never silently drop audit events.
_MAX_JSON = 96 * 1024


def _private_root(path: Path) -> Path:
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise ScopeDeniedError("operator audit root must be a real provisioned absolute directory")
    absolute = path.resolve(strict=True)
    if absolute != path or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise ScopeDeniedError("operator audit root must have exact 0700 permissions")
    return absolute


def _safe_write(path: Path, value: dict[str, Any]) -> None:
    """Atomic, mode-0600, no-follow replacement in an operator-private directory."""
    data = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if len(data) > _MAX_JSON:
        raise CapabilityUnavailableError("telemetry state exceeds safety limit")
    temporary = path.with_name("." + path.name + "." + uuid4().hex)
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if path.is_symlink():
            raise ScopeDeniedError("operator telemetry path redirected")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_read(path: Path) -> dict[str, Any]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fileinfo = os.fstat(fd)
        if not stat.S_ISREG(fileinfo.st_mode) or fileinfo.st_size > _MAX_JSON or fileinfo.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ScopeDeniedError("telemetry file permissions or size invalid")
        raw = os.read(fd, _MAX_JSON + 1)
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("telemetry envelope is not an object")
        return result
    finally:
        os.close(fd)


def _counter_file(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        tokens = line.split()
        if len(tokens) == 2 and re.fullmatch(r"[a-z_]+", tokens[0]) and tokens[1].isdigit():
            result[tokens[0]] = int(tokens[1])
    return result


def _cgroup_usage(path: Path) -> dict[str, int]:
    cpu = _counter_file(path / "cpu.stat")
    required = {"usage_usec", "user_usec", "system_usec"}
    if not required.issubset(cpu):
        raise CapabilityUnavailableError("cgroup-v2 CPU accounting unavailable")
    io_read = io_write = 0
    io_path = path / "io.stat"
    if not io_path.is_file():
        raise CapabilityUnavailableError("cgroup-v2 IO accounting unavailable")
    for line in io_path.read_text(encoding="ascii").splitlines():
        for part in line.split()[1:]:
            key, sep, raw = part.partition("=")
            if sep and raw.isdigit():
                if key == "rbytes": io_read += int(raw)
                elif key == "wbytes": io_write += int(raw)
    def scalar(name: str) -> int:
        raw = (path / name).read_text(encoding="ascii").strip()
        if not raw.isdigit():
            raise CapabilityUnavailableError(f"invalid cgroup-v2 {name}")
        return int(raw)
    return {"cpu_usec": cpu["usage_usec"], "user_usec": cpu["user_usec"],
            "system_usec": cpu["system_usec"], "read_bytes": io_read,
            "write_bytes": io_write, "memory_current_bytes": scalar("memory.current"),
            "memory_peak_bytes": scalar("memory.peak"), "pids_current": scalar("pids.current")}


def _proc_details(pid: int) -> dict[str, int] | None:
    """Include starttime to distinguish PID reuse; no cmdline or environment."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw.rsplit(") ", 1)[1].split()
        # /proc stat fields 3... are indexed from zero here.
        result = {"pid": pid, "ppid": int(fields[1]), "start_ticks": int(fields[19]),
                  "cpu_ticks_sampled": int(fields[11]) + int(fields[12]),
                  "rss_pages_sampled": int(fields[21])}
        io = _counter_file(Path(f"/proc/{pid}/io"))
        result["read_bytes_sampled"] = io.get("read_bytes", 0)
        result["write_bytes_sampled"] = io.get("write_bytes", 0)
        return result
    except (OSError, IndexError, ValueError):
        return None  # PID already exited; syscall trace still records its lifetime.


def _trace_events(path: Path, pid: int, events: Any, calls: Counter[str],
                  *, integrity: dict[str, int] | None = None,
                  timing: dict[str, int] | None = None) -> int:
    """Reassemble strace's unfinished/resumed lines before classifying access.

    strace -ff traces one task per file. A resumed syscall has no pathname in
    its continuation; classifying it alone drops the original file access.
    Unpaired continuations are signalled as audit-incomplete, never discarded.
    """
    count = 0
    pending: tuple[str, str, str] | None = None
    malformed = 0
    total_duration_us = 0
    with path.open("r", encoding="utf-8", errors="replace") as raw:
        for line in raw:
            match = _TRACE_LINE.match(line)
            resumed = _TRACE_RESUMED.match(line)
            if match:
                syscall = match["call"]
                if _UNFINISHED in line:
                    if pending is not None:
                        malformed += 1
                    head = line.split(_UNFINISHED, 1)[0]
                    pending = (syscall, match["stamp"], head[match.start("call"):])
                    continue
                if pending is not None:
                    # Seeing a new entry without the prior continuation is
                    # evidence of a missing/unparsable trace line.
                    malformed += 1
                    pending = None
                assembled = line[match.start("call"):]
                stamp = match["stamp"]
            elif resumed:
                syscall = resumed["call"]
                if pending is None or pending[0] != syscall:
                    malformed += 1
                    continue
                stamp = pending[1]
                assembled = pending[2] + resumed["rest"]
                pending = None
            else:
                # Signals and exit markers are not syscall completions.
                continue
            calls[syscall] += 1
            duration = _TRACE_END_TIME.search(assembled)
            if duration:
                total_duration_us += int(round(float(duration["seconds"]) * 1_000_000))
            mode: str | None = None
            if syscall in {"open", "openat", "openat2", "creat"}:
                mode = ("read_write" if "O_RDWR" in assembled else "write" if any(
                    flag in assembled for flag in ("O_WRONLY", "O_CREAT", "O_TRUNC", "O_APPEND")
                ) or syscall == "creat" else "read")
            elif syscall in {"execve", "execveat", "fexecve"}:
                mode = "execute"
            elif syscall in {"read", "readv", "pread64", "preadv", "preadv2", "sendfile", "splice", "copy_file_range"}:
                mode = "read"
            elif syscall in {"write", "writev", "pwrite64", "pwritev", "pwritev2", "ftruncate", "fsync", "fdatasync", "fallocate"}:
                mode = "write"
            elif syscall in {"unlink", "unlinkat", "rename", "renameat", "renameat2", "mkdir", "mkdirat", "rmdir", "chmod", "fchmod", "fchmodat", "chown", "fchown", "fchownat", "link", "linkat", "symlink", "symlinkat", "truncate", "utimensat"}:
                mode = "metadata_write"
            elif syscall in {"stat", "statx", "lstat", "fstat", "fstatat", "newfstatat", "access", "faccessat", "readlink", "readlinkat", "getdents", "getdents64", "name_to_handle_at"}:
                mode = "metadata_read"
            elif syscall in {"mmap", "mmap2"}:
                if "MAP_SHARED" in assembled and "PROT_WRITE" in assembled:
                    mode = "write"
                elif "PROT_READ" in assembled or "PROT_EXEC" in assembled:
                    mode = "read"
            if mode is None:
                continue
            fd = _FD_PATH.search(assembled)
            # The first quoted string is a pathname for path-based calls, and
            # a user buffer for read/write (hence use the decoded -yy fd there).
            quoted = _STRING.findall(assembled)
            path_syscalls = {"open", "openat", "openat2", "creat", "execve", "execveat",
                "stat", "statx", "lstat", "fstatat", "newfstatat", "access", "faccessat",
                "readlink", "readlinkat", "unlink", "unlinkat", "rename", "renameat",
                "renameat2", "mkdir", "mkdirat", "rmdir", "chmod", "chown", "link",
                "linkat", "symlink", "symlinkat", "truncate", "utimensat", "name_to_handle_at"}
            paths: list[tuple[str, str | None]] = []
            if syscall in path_syscalls and quoted:
                # Symlink's target is only stored as text; it is not a file
                # accessed by this syscall. Audit the created link, not target.
                if syscall in {"symlink", "symlinkat"}:
                    tokens = [(1, quoted[1])] if len(quoted) >= 2 else []
                else:
                    tokens = list(enumerate(quoted[:2] if syscall in _DUAL_PATH else quoted[:1]))
                for index, token in tokens:
                    try:
                        pathname = ast.literal_eval(token)
                    except (ValueError, SyntaxError):
                        continue
                    if isinstance(pathname, str) and pathname:
                        role = ("source" if index == 0 else "destination") if syscall in _DUAL_PATH else (
                            "destination" if syscall in {"symlink", "symlinkat"} else None)
                        paths.append((pathname, role))
            elif fd:
                paths = [(fd.group(1), None)]
            if not paths and fd:
                paths = [(fd.group(1), None)]
            for pathname, role in paths:
                record = {"pid": pid, "time": float(stamp), "syscall": syscall,
                          "path": pathname[:8192], "mode": mode,
                          "result_error": " = -1 " in assembled}
                if role is not None:
                    record["path_role"] = role
                events.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
                count += 1
    if pending is not None:
        malformed += 1
    if integrity is not None:
        integrity["unpaired_syscalls"] = integrity.get("unpaired_syscalls", 0) + malformed
    if timing is not None:
        timing["syscall_wall_usec"] = total_duration_us
    return count


@dataclass(frozen=True, slots=True)
class WorkerMonitorConfig:
    audit_root: Path  # outside every guest mount, operator owned (0700)
    cgroup_root: Path  # delegated, controller-enabled cgroup v2 subtree
    strace: Path        # pinned host binary, never a guest-provided executable
    cgroup_join: Path   # operator-owned native helper, no Python preexec_fn
    strace_sha256: str  # image-qualified digest, before every launch
    cgroup_join_sha256: str
    max_trace_bytes: int = _MAX_TRACE
    sample_seconds: float = 0.2

    def __post_init__(self) -> None:
        _private_root(self.audit_root)
        if not (self.cgroup_root.is_absolute() and self.cgroup_root.is_dir()
                and not self.cgroup_root.is_symlink() and self.cgroup_root.resolve(strict=True) == self.cgroup_root
                and (self.cgroup_root / "cgroup.controllers").exists()):
            raise CapabilityUnavailableError("delegated cgroup v2 root unavailable")
        for executable, expected in ((self.strace, self.strace_sha256),
                                     (self.cgroup_join, self.cgroup_join_sha256)):
            if (not isinstance(executable, Path) or not executable.is_absolute()
                    or not executable.is_file() or executable.is_symlink()
                    or executable.resolve(strict=True) != executable
                    or not os.access(executable, os.X_OK)
                    or executable.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                    or not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected)):
                raise CapabilityUnavailableError("operator monitoring executable unqualified")
            # Recheck immediately before launch to reject a replaced binary.
            with executable.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise CapabilityUnavailableError("operator monitoring executable differs from image pin")
        if not 1024 * 1024 <= self.max_trace_bytes <= 4 * 1024**3 or not .05 <= self.sample_seconds <= 2:
            raise ValueError("worker monitor limits invalid")

    @classmethod
    def from_environment(cls) -> WorkerMonitorConfig:
        root, cgroup, tracer, helper, tracer_sha, helper_sha = (os.environ.get(key) for key in (
            "POTBOT_OPERATOR_AUDIT_ROOT", "POTBOT_OPERATOR_CGROUP_ROOT", "POTBOT_OPERATOR_STRACE",
            "POTBOT_OPERATOR_CGROUP_JOIN", "POTBOT_OPERATOR_STRACE_SHA256",
            "POTBOT_OPERATOR_CGROUP_JOIN_SHA256"))
        if not all((root, cgroup, tracer, helper, tracer_sha, helper_sha)):
            raise CapabilityUnavailableError("operator telemetry binaries, pinned digests and roots must be configured")
        return cls(Path(root), Path(cgroup), Path(tracer), Path(helper), tracer_sha, helper_sha)


class WorkerMonitorSession:
    """One process tree: host-owned cgroup + secure trace files + live snapshot."""
    def __init__(self, config: WorkerMonitorConfig, workspace_id: UUID,
                 agent_id: UUID, image_identity: str) -> None:
        self.config, self.workspace_id, self.agent_id = config, workspace_id, agent_id
        self.worker_id = uuid4()
        self.image_identity = image_identity
        self._path = config.audit_root / str(workspace_id)
        self._path.mkdir(mode=0o700, exist_ok=True)
        if self._path.is_symlink() or self._path.stat().st_mode & 0o077:
            raise ScopeDeniedError("workspace telemetry directory must be private")
        self.path = self._path / str(self.worker_id)
        self.path.mkdir(mode=0o700)
        self.cgroup = config.cgroup_root / self.worker_id.hex
        self.cgroup.mkdir(mode=0o755)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fault: str | None = None
        self._processes: dict[str, dict[str, int]] = {}
        self._last_usage: dict[str, int] = {}
        self._started_at = time.time()
        self._start_pid: int | None = None
        self._live = self.path / "live.json"
        try:
            _cgroup_usage(self.cgroup)  # Reject unaccounted cgroup before launch.
            _safe_write(self.path / "identity.json", {
                "schema": 1, "workspace_id": str(workspace_id), "agent_id": str(agent_id),
                "worker_id": str(self.worker_id), "image_identity": image_identity,
                "started_at": self._started_at,
            })
        except BaseException:
            self.cgroup.rmdir()
            raise

    def command(self, argv: tuple[str, ...]) -> tuple[str, ...]:
        # Requalify hashes at launch, not merely when the monitor was created.
        for executable, expected in ((self.config.strace, self.config.strace_sha256),
                                     (self.config.cgroup_join, self.config.cgroup_join_sha256)):
            with executable.open("rb") as file:
                if hashlib.file_digest(file, "sha256").hexdigest() != expected:
                    raise CapabilityUnavailableError("operator executable pin changed before launch")
        return (str(self.config.cgroup_join), str(self.cgroup), "--",
                str(self.config.strace), "-f", "-ff", "-qq", "-ttt", "-T", "-yy",
                "-s", "4096", "-o", str(self.path / "trace"), "--", *argv)

    def start(self, pid: int) -> None:
        self._start_pid = pid
        self._thread = threading.Thread(target=self._poll, name="potbot-operator-cgroup-audit", daemon=True)
        self._thread.start()

    @property
    def healthy(self) -> bool:
        return self._fault is None and self._thread is not None and self._thread.is_alive()

    def _sample(self) -> None:
        self._last_usage = _cgroup_usage(self.cgroup)
        for token in (self.cgroup / "cgroup.procs").read_text(encoding="ascii").splitlines():
            if not _PID.fullmatch(token):
                raise CapabilityUnavailableError("invalid cgroup process identifier")
            pid = int(token)
            sample = _proc_details(pid)
            if sample is None:
                continue
            key = f"{pid}:{sample['start_ticks']}"
            previous = self._processes.get(key)
            if previous is None:
                self._processes[key] = sample
            else:
                for label in ("cpu_ticks_sampled", "rss_pages_sampled", "read_bytes_sampled", "write_bytes_sampled"):
                    previous[label] = max(previous[label], sample[label])
                previous["ppid"] = sample["ppid"]
        _safe_write(self._live, {
            "schema": 1, "state": "running", "workspace_id": str(self.workspace_id),
            "agent_id": str(self.agent_id), "worker_id": str(self.worker_id),
            "pid": self._start_pid, "sampled_at": time.time(), "usage": self._last_usage,
            "observed_processes": len(self._processes),
        })
        total = sum(p.stat().st_size for p in self.path.glob("trace.*") if p.is_file())
        if total > self.config.max_trace_bytes:
            raise CapabilityUnavailableError("complete syscall audit exceeds configured byte ceiling")

    def _poll(self) -> None:
        try:
            while not self._stop.is_set():
                self._sample()
                self._stop.wait(self.config.sample_seconds)
        except BaseException as exc:
            self._fault = type(exc).__name__  # Do not publish privileged paths.

    def finish(self, *, confirmed_stopped: bool) -> bool:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
            if self._thread.is_alive(): self._fault = "monitor_unresponsive"
        try:
            # Terminate any detached descendants by *cgroup identity*, never by
            # a potentially recycled PID, before taking lifetime counters.
            if confirmed_stopped and (self.cgroup / "cgroup.procs").read_text().strip():
                fd = os.open(self.cgroup / "cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try: os.write(fd, b"1\n")
                finally: os.close(fd)
                for _ in range(40):
                    if not (self.cgroup / "cgroup.procs").read_text().strip(): break
                    time.sleep(0.025)
            self._sample()
            traces = sorted(self.path.glob("trace.*"))
            total_size = sum(p.stat().st_size for p in traces)
            if not traces or total_size > self.config.max_trace_bytes:
                raise CapabilityUnavailableError("worker trace absent or over byte quota")
            counts: Counter[str] = Counter()
            integrity: dict[str, int] = {}
            per_pid: dict[str, dict[str, int]] = {}
            event_path = self.path / "file-events.jsonl"
            fd = os.open(event_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                event_count = 0
                for path in traces:
                    if not _PID.fullmatch(path.suffix[1:]):
                        integrity["invalid_trace_filenames"] = integrity.get("invalid_trace_filenames", 0) + 1
                        continue
                    pid = int(path.suffix[1:])
                    per_calls: Counter[str] = Counter()
                    timing: dict[str, int] = {}
                    entries = _trace_events(path, pid, output, per_calls, integrity=integrity, timing=timing)
                    event_count += entries
                    counts.update(per_calls)
                    per_pid[str(pid)] = {"completed_syscalls": sum(per_calls.values()),
                                         "file_events": entries, **timing}
                output.flush()
                os.fsync(output.fileno())
            sha = hashlib.sha256()
            for path in traces:
                with path.open("rb") as stream:
                    while (block := stream.read(1048576)): sha.update(block)
            self._last_usage = _cgroup_usage(self.cgroup)
            complete = confirmed_stopped and self._fault is None and not any(integrity.values())
            _safe_write(self.path / "lifetime.json", {
                "schema": 1, "workspace_id": str(self.workspace_id), "agent_id": str(self.agent_id),
                "worker_id": str(self.worker_id), "started_at": self._started_at,
                "finished_at": time.time(), "audit_complete": complete,
                "audit_failure": self._fault, "syscall_counts": dict(counts),
                "file_event_count": event_count, "trace_sha256": sha.hexdigest(),
                "trace_integrity": integrity,
                "per_pid_syscall_lifetime": per_pid,
                "per_pid_syscall_wall_time_is_not_cpu_time": True,
                "traced_pid_count": len(traces),
                "traced_pids": [int(p.suffix[1:]) for p in traces if _PID.fullmatch(p.suffix[1:])],
                "sampled_processes": self._processes,
                "sampled_pid_resources_are_lower_bounds": True,
                "cgroup_lifetime_usage_includes_tracer": True,
                "cgroup_usage": self._last_usage,
            })
            _safe_write(self._live, {
                "schema": 1, "state": "finished", "workspace_id": str(self.workspace_id),
                "agent_id": str(self.agent_id), "worker_id": str(self.worker_id),
                "sampled_at": time.time(), "usage": self._last_usage,
            })
        except BaseException as exc:
            self._fault = self._fault or type(exc).__name__
            complete = False
            # Persist a fail-closed marker, but never fabricate complete audit.
            try:
                _safe_write(self._live, {"schema": 1, "state": "fault", "workspace_id": str(self.workspace_id),
                    "agent_id": str(self.agent_id), "worker_id": str(self.worker_id), "sampled_at": time.time(),
                    "usage": self._last_usage, "fault": self._fault})
            except OSError:
                pass
        # A populated cgroup is never silently released or reused.
        try:
            if confirmed_stopped and not (self.cgroup / "cgroup.procs").read_text().strip():
                self.cgroup.rmdir()
            else:
                complete = False
        except OSError:
            complete = False
        return complete


@dataclass
class _UtilisationWindow:
    """Time-weighted window averages over observed host CPU deltas."""
    samples: deque[tuple[float, float]] = field(default_factory=deque)

    def put(self, stamp: float, fraction: float) -> None:
        self.samples.append((stamp, max(0.0, min(1.0, fraction))))
        while len(self.samples) > 1 and self.samples[1][0] < stamp - 180:
            self.samples.popleft()

    def average(self, seconds: float) -> float:
        if not self.samples: return 0.0
        cutoff = self.samples[-1][0] - seconds
        sample = list(self.samples)
        numerator = denominator = 0.0
        for i in range(1, len(sample)):
            left, right = sample[i-1], sample[i]
            start = max(cutoff, left[0])
            weight = max(0.0, right[0] - start)
            numerator += right[1] * weight
            denominator += weight
        return numerator / denominator if denominator else sample[-1][1]


def _cpu_stat() -> tuple[int, int]:
    cpu = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()
    if cpu[0] != "cpu" or len(cpu) < 9:
        raise CapabilityUnavailableError("Linux host CPU counters unavailable")
    ticks = [int(x) for x in cpu[1:]]
    total = sum(ticks)
    idle = ticks[3] + ticks[4]
    return total, total - idle


def _psi(resource: str) -> dict[str, float]:
    result: dict[str, float] = {}
    try:
        for line in Path("/proc/pressure", resource).read_text().splitlines():
            tokens = line.split()
            if tokens[0] == "some":
                for part in tokens[1:]:
                    k, sep, value = part.partition("=")
                    if k in {"avg10", "avg60", "avg300"} and sep:
                        result[k] = max(0.0, min(100.0, float(value)))
    except (OSError, ValueError, IndexError):
        pass
    return result


def _composite_load(cpu_fraction: float, pressure: dict[str, dict[str, float]]) -> float:
    """0–100% aggregate saturation from busy CPU or Linux scheduler stalls.

    PSI reports time stalled (CPU contention, memory reclaim or I/O latency).
    Use max rather than sum to avoid double-counting correlated bottlenecks.
    This is a conservative pressure indicator, not a physical capacity metric.
    """
    components = [cpu_fraction]
    for kind in ("cpu", "memory", "io"):
        components.append(pressure.get(kind, {}).get("avg10", 0.0) / 100.0)
    return max(0.0, min(1.0, max(components)))


def _mem_fraction() -> float:
    entries: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit(): entries[parts[0].rstrip(":")] = int(parts[1])
    total, free = entries["MemTotal"], entries["MemAvailable"]
    return max(0.0, min(1.0, 1.0 - free / total)) if total else 0.0


class OperatorTelemetryReader:
    """Read-only, workspace-scoped projection of private host samples.

    UI never receives raw traces, filenames, paths, command lines or other
    workspaces' agent IDs. Requires a provisioned operator-owned audit directory.
    """
    def __init__(self, root: Path) -> None:
        self.root = _private_root(root)
        self._mutex = threading.Lock()
        self._previous_heat_cpu: dict[str, tuple[int, int]] = {}
        self._previous_workers: dict[str, int] = {}
        self._window = _UtilisationWindow()
        self._last_host_sample = 0.0
        self._stop = threading.Event()
        self._sampler = threading.Thread(target=self._sample_host, daemon=True,
                                         name="potbot-host-utilisation-sampler")
        self._sampler.start()

    def close(self) -> None:
        self._stop.set()
        self._sampler.join(timeout=2)

    def _sample_host(self) -> None:
        previous: tuple[int, int] | None = None
        while not self._stop.is_set():
            try:
                cpu = _cpu_stat()
                now = time.monotonic()
                with self._mutex:
                    if previous is not None:
                        total = cpu[0] - previous[0]
                        if total > 0:
                            busy = max(0, cpu[1] - previous[1]) / total
                            pressure = {name: _psi(name) for name in ("cpu", "io", "memory")}
                            self._window.put(now, _composite_load(busy, pressure))
                            self._last_host_sample = now
                previous = cpu
            except (OSError, ValueError, CapabilityUnavailableError):
                pass  # stale host readings are explicitly marked unavailable
            self._stop.wait(.5)

    def snapshot(self, workspace_id: UUID) -> dict[str, Any]:
        if not isinstance(workspace_id, UUID):
            raise ScopeDeniedError("workspace telemetry requires trusted UUID scope")
        with self._mutex:
            now = time.monotonic()
            cpu = _cpu_stat()
            host_busy_delta = 0
            key = str(workspace_id)
            previous = self._previous_heat_cpu.get(key)
            if previous is not None:
                host_busy_delta = max(0, cpu[1] - previous[1])
            self._previous_heat_cpu[key] = cpu
            workers: list[dict[str, Any]] = []
            agent_share: dict[str, float] = {}
            workspace = self.root / str(workspace_id)
            if workspace.is_dir() and not workspace.is_symlink():
                for folder in list(workspace.iterdir())[:256]:
                    if not folder.is_dir() or folder.is_symlink(): continue
                    try: item = _safe_read(folder / "live.json")
                    except (OSError, ValueError, ScopeDeniedError): continue
                    if item.get("workspace_id") != str(workspace_id) or item.get("state") != "running": continue
                    if not isinstance(item.get("sampled_at"), (int, float)) or time.time() - item["sampled_at"] > 5: continue
                    agent = item.get("agent_id")
                    if not isinstance(agent, str): continue
                    try: UUID(agent)
                    except ValueError: continue
                    usage = item.get("usage")
                    if not isinstance(usage, dict) or type(usage.get("cpu_usec")) is not int: continue
                    worker_key = str(item.get("worker_id"))
                    value = usage["cpu_usec"]
                    before = self._previous_workers.get(worker_key)
                    delta = max(0, value - before) if before is not None else 0
                    self._previous_workers[worker_key] = value
                    workers.append({"workerId": worker_key, "agentId": agent,
                                    "pidCount": min(4096, max(0, int(usage.get("pids_current", 0)))),
                                    "memoryBytes": max(0, int(usage.get("memory_current_bytes", 0)))})
                    agent_share[agent] = agent_share.get(agent, 0) + delta
            # CPU host 'busy' is summed over all logical CPUs; both counters
            # are durations. cgroup time includes only observed traced subtree.
            host_usec = host_busy_delta * 1_000_000 / os.sysconf("SC_CLK_TCK")
            agents = [{"agentId": key, "heat": round(max(0.0, min(1.0, value / host_usec)), 4)
                       if host_usec > 0 else 0.0}
                      for key, value in sorted(agent_share.items())]
            return {"schema": 1, "available": bool(self._window.samples)
                    and now - self._last_host_sample < 3.0,
                    "system": {"load3m": round(self._window.average(180), 4),
                               "load2s": round(self._window.average(2), 4),
                               "memoryUsed": round(_mem_fraction(), 4),
                               "pressure": {name: _psi(name) for name in ("cpu", "io", "memory")},
                               "logicalCpus": os.cpu_count() or 1},
                    "workers": workers, "agents": agents,
                    "audit": {"sampling": "on-demand", "individualPidCpu": "sampled_lower_bound"}}
