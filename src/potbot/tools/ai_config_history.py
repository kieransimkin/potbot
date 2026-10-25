"""Operator-owned /etc/ai history journal; guests only get a read-only view.

Linux inotify triggers reconciliation of durable *filesystem states*, not a
claim of byte-perfect capture of every intermediate write syscall. Each
transition gets an exact inverse manifest (including deleted/binary contents)
plus a human-readable reverse diff. All journal data lives OUTSIDE guest-writeable
/etc/ai. A failed watcher, overflow, ambiguous tree or storage failure fences
new sandbox launches until explicit operator reconciliation.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import difflib
import hashlib
import json
import os
import select
import stat
import struct
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError, StateMismatchError

# Linux inotify. Include writes, rename, creation, deletion and metadata edits.
_IN_MODIFY = 0x00000002
_IN_ATTRIB = 0x00000004
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_FROM = 0x00000040
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_DELETE_SELF = 0x00000400
_IN_MOVE_SELF = 0x00000800
_IN_Q_OVERFLOW = 0x00004000
_IN_IGNORED = 0x00008000
_IN_ISDIR = 0x40000000
_MASK = (_IN_MODIFY | _IN_ATTRIB | _IN_CLOSE_WRITE | _IN_MOVED_FROM | _IN_MOVED_TO
         | _IN_CREATE | _IN_DELETE | _IN_DELETE_SELF | _IN_MOVE_SELF)
_EVENT = struct.Struct("iIII")
_MAX_EVENTS = 8192


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def _relative(value: str) -> PurePosixPath:
    if (not isinstance(value, str) or not value or "\\" in value or "\x00" in value
            or value.startswith("/") or any(p in ("", ".", "..") for p in value.split("/"))):
        raise ScopeDeniedError("history entry has an unsafe relative path")
    return PurePosixPath(value)


def _physical_directory(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ScopeDeniedError("AI directory must be a provisioned physical absolute directory")
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise ScopeDeniedError("AI mount sources may not traverse symlinked ancestors")
    return resolved


def _scan(root: Path, *, max_files: int, max_bytes: int,
          max_file_bytes: int) -> dict[str, dict[str, Any]]:
    """Snapshot with dirfd-relative traversal, never following a raced parent link.

    Every ancestor is held open by file descriptor and checked before/after
    traversal. Path-based ``os.walk`` plus O_NOFOLLOW only on a leaf does NOT
    prevent a concurrently replaced parent directory escaping /etc/ai.
    """
    entries: dict[str, dict[str, Any]] = {}
    total = 0
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

    def same_entry(a: os.stat_result, b: os.stat_result) -> bool:
        return (a.st_dev, a.st_ino, a.st_mode, a.st_size, a.st_mtime_ns) == (
            b.st_dev, b.st_ino, b.st_mode, b.st_size, b.st_mtime_ns
        )

    def walk(parent_fd: int, prefix: str) -> None:
        nonlocal total
        original = os.fstat(parent_fd)
        for name in sorted(os.listdir(parent_fd)):
            relative = name if not prefix else f"{prefix}/{name}"
            _relative(relative)
            if len(entries) >= max_files:
                raise CapabilityUnavailableError("AI configuration file count exceeded")
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISDIR(before.st_mode):
                try:
                    child_fd = os.open(name, directory_flags, dir_fd=parent_fd)
                except OSError as exc:
                    raise StateMismatchError("AI config directory changed during snapshot") from exc
                try:
                    if not same_entry(before, os.fstat(child_fd)):
                        raise StateMismatchError("AI config directory was replaced during snapshot")
                    entries[relative] = {"type": "dir", "mode": stat.S_IMODE(before.st_mode)}
                    walk(child_fd, relative)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(before.st_mode):
                # Hardlinked files can be altered through a path outside the
                # monitored tree without emitting inotify events under /etc/ai.
                if before.st_nlink != 1:
                    raise ScopeDeniedError("AI configuration files must not have hardlinks")
                if before.st_size > max_file_bytes or total + before.st_size > max_bytes:
                    raise CapabilityUnavailableError("AI configuration byte budget exceeded")
                try:
                    fd = os.open(name, file_flags, dir_fd=parent_fd)
                except OSError as exc:
                    raise StateMismatchError("AI config file changed during snapshot") from exc
                try:
                    if not same_entry(before, os.fstat(fd)):
                        raise StateMismatchError("AI config file changed before reading")
                    data = bytearray()
                    while len(data) <= max_file_bytes:
                        part = os.read(fd, min(65536, max_file_bytes - len(data) + 1))
                        if not part:
                            break
                        data.extend(part)
                    after = os.fstat(fd)
                    if (len(data) != before.st_size or len(data) > max_file_bytes
                            or not same_entry(before, after)):
                        raise StateMismatchError("AI configuration mutated while reading")
                finally:
                    os.close(fd)
                total += len(data)
                payload = bytes(data)
                entries[relative] = {
                    "type": "file", "mode": stat.S_IMODE(before.st_mode),
                    "sha256": _sha(payload),
                    "data_base64": base64.b64encode(payload).decode("ascii"),
                }
            else:
                raise ScopeDeniedError("AI configuration may contain only regular files/directories")
            try:
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise StateMismatchError("AI configuration entry vanished during snapshot") from exc
            if not same_entry(before, current):
                raise StateMismatchError("AI configuration entry changed during snapshot")
        if not same_entry(original, os.fstat(parent_fd)):
            raise StateMismatchError("AI configuration directory changed during traversal")

    # The host preflights the physical source. Retain an FD to the root so a
    # concurrent rename cannot change which tree is being traversed.
    fd = os.open(root, directory_flags)
    try:
        before = os.fstat(fd)
        walk(fd, "")
        try:
            after = os.stat(root, follow_symlinks=False)
        except OSError as exc:
            raise StateMismatchError("AI config root vanished during snapshot") from exc
        if not same_entry(before, after):
            raise StateMismatchError("AI configuration root was replaced during snapshot")
    finally:
        os.close(fd)
    return dict(sorted(entries.items()))


def _identity(snapshot: Mapping[str, dict[str, Any]]) -> str:
    """Identity binds content, permissions, and layout without relying on mtime."""
    public = {name: {k: v for k, v in detail.items() if k != "data_base64"}
              for name, detail in snapshot.items()}
    return _sha(_json_bytes(public))


def _render_reverse_diff(changes: list[dict[str, Any]], sequence: int) -> str:
    lines = [f"# Potbot /etc/ai reverse patch, revision {sequence}\n",
             "# Apply the matching .reverse.json with an operator-approved restore tool.\n"]
    for change in changes:
        name = change["path"]
        before, after = change["before"], change["after"]
        old_data = base64.b64decode(before["data_base64"]) if before and before["type"] == "file" else b""
        new_data = base64.b64decode(after["data_base64"]) if after and after["type"] == "file" else b""
        if before and before["type"] == "dir" or after and after["type"] == "dir":
            lines.append(f"# directory {name}: {after and after['type']} -> {before and before['type']}\n")
            continue
        try:
            old_text, new_text = old_data.decode("utf-8"), new_data.decode("utf-8")
            if "\x00" in old_text + new_text:
                raise UnicodeError
        except UnicodeError:
            lines.append(f"# binary {name}: inverse payload in matching .reverse.json\n")
            continue
        a = new_text.splitlines(keepends=True)
        b = old_text.splitlines(keepends=True)
        lines.extend(difflib.unified_diff(a, b, fromfile=f"a/etc/ai/{name}",
                                          tofile=f"b/etc/ai/{name}"))
        if old_text == new_text and before != after:
            lines.append(f"# mode-only change {name}: {after and after['mode']} -> {before and before['mode']}\n")
    return "".join(lines)


def reconstruct_prior_snapshot(
    current: Mapping[str, dict[str, Any]], inverse: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Validate and apply an inverse record IN MEMORY; never mutate the guest mount.

    A future privileged restore operation must separately quiesce guest writers
    and atomically apply this checked snapshot through a qualified host broker.
    """
    if inverse.get("schema") != "potbot-etc-ai-reversal/v1":
        raise InvalidInputError("unsupported AI reverse record schema")
    if _identity(current) != inverse.get("from_sha256"):
        raise StateMismatchError("live AI tree does not match reverse record precondition")
    changes = inverse.get("changes")
    if not isinstance(changes, list) or len(changes) > 20000:
        raise InvalidInputError("AI reversal change set is invalid")
    prior = dict(current)
    seen: set[str] = set()
    for item in changes:
        if not isinstance(item, dict):
            raise InvalidInputError("AI reversal entry is not an object")
        name = item.get("path")
        _relative(name)
        if name in seen or item.get("after") != prior.get(name):
            raise StateMismatchError("AI reversal does not match this filesystem state")
        seen.add(name)
        old = item.get("before")
        if old is None:
            prior.pop(name, None)
        elif isinstance(old, dict) and old.get("type") in {"file", "dir"}:
            if old["type"] == "file":
                try:
                    raw = base64.b64decode(old["data_base64"], validate=True)
                except (ValueError, KeyError) as exc:
                    raise InvalidInputError("reversal file payload is invalid") from exc
                if _sha(raw) != old.get("sha256"):
                    raise StateMismatchError("AI reversal file bytes fail their digest")
            prior[name] = old
        else:
            raise InvalidInputError("invalid AI reversal before-state")
    if _identity(prior) != inverse.get("restore_sha256"):
        raise StateMismatchError("AI reversal does not reconstruct the prior root identity")
    return dict(sorted(prior.items()))


@dataclass(frozen=True, slots=True)
class AiReverseRevision:
    sequence: int
    before_digest: str
    after_digest: str
    inverse_path: Path
    text_diff_path: Path
    observed_events: int


class AiConfigHistoryMonitor:
    """One controller process owns the inotify FD, snapshot and durable history.

    Call start() before exposing the guest mount. No process may write history.
    The controller checks require_active() before each new guest launch. It must
    keep this monitor alive for the process lifetime and stop launches on faults.
    """

    def __init__(self, *, config_dir: Path, past_dir: Path, max_files: int = 2048,
                 max_bytes: int = 16 * 1024 * 1024, max_file_bytes: int = 8 * 1024 * 1024,
                 interval_seconds: float = 0.15) -> None:
        if sys.platform != "linux":
            raise CapabilityUnavailableError("AI config history requires Linux inotify")
        self.config_dir = _physical_directory(config_dir)
        self.past_dir = _physical_directory(past_dir)
        if (self.past_dir == self.config_dir or self.past_dir.is_relative_to(self.config_dir)
                or self.config_dir.is_relative_to(self.past_dir)):
            raise ScopeDeniedError("AI configuration and immutable past must be physically disjoint")
        # The guest sees this data through a read-only bind, but the host-side
        # backing store must not be writable by arbitrary workspace members.
        if self.past_dir.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ScopeDeniedError("AI past must be an operator-owned non-group-writable store")
        if (not 1 <= max_files <= 20000 or not 1 <= max_file_bytes <= max_bytes <= 256 * 1024 * 1024
                or not 0.01 <= interval_seconds <= 10):
            raise InvalidInputError("AI history resource limits are invalid")
        self.max_files, self.max_bytes, self.max_file_bytes = max_files, max_bytes, max_file_bytes
        self.interval_seconds = interval_seconds
        self._mutex = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fd: int | None = None
        self._lock_fd: int | None = None
        self._watch_dirs: dict[int, str] = {}
        self._snapshot: dict[str, dict[str, Any]] = {}
        self._sequence = 0
        self._fault: str | None = "not started"
        self._started = False
        self._libc: Any = None

    def _capture(self) -> dict[str, dict[str, Any]]:
        return _scan(self.config_dir, max_files=self.max_files,
                     max_bytes=self.max_bytes, max_file_bytes=self.max_file_bytes)

    def _write_once(self, filename: str, payload: bytes) -> Path:
        target = self.past_dir / filename
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
        with os.fdopen(fd, "wb") as writer:
            writer.write(payload)
            writer.flush()
            os.fsync(writer.fileno())
        return target

    def _replace_head(self, payload: dict[str, Any]) -> None:
        # Never follow a guest symlink or silently overwrite an unexpected file.
        pending = self._write_once(f".head-{self._sequence:012d}.tmp", _json_bytes(payload))
        os.replace(pending, self.past_dir / "head.json")
        directory_fd = os.open(self.past_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _add_watches(self) -> None:
        assert self._fd is not None and self._libc is not None
        existing = set(self._watch_dirs.values())
        for parent, dirs, _ in os.walk(self.config_dir, followlinks=False):
            base = Path(parent)
            if base.is_symlink():
                raise ScopeDeniedError("AI config cannot contain symlinked watch directories")
            relative = "." if base == self.config_dir else base.relative_to(self.config_dir).as_posix()
            if relative in existing:
                continue
            wd = self._libc.inotify_add_watch(self._fd, os.fsencode(base), _MASK)
            if wd < 0:
                raise CapabilityUnavailableError("inotify could not cover all AI config directories")
            self._watch_dirs[wd] = relative
            existing.add(relative)
            if len(self._watch_dirs) > self.max_files + 1:
                raise CapabilityUnavailableError("AI config watcher limit exceeded")

    def start(self) -> None:
        import fcntl  # Linux-only: do not break importing the controller on Windows.

        with self._mutex:
            if self._started:
                raise StateMismatchError("AI configuration watcher already started")
            if self._fault != "not started":
                raise StateMismatchError("faulted AI watcher requires fresh operator reconciliation")
            libc_name = ctypes.util.find_library("c")
            if libc_name is None:
                raise CapabilityUnavailableError("Linux libc inotify unavailable")
            self._libc = ctypes.CDLL(libc_name, use_errno=True)
            self._libc.inotify_init1.argtypes = [ctypes.c_int]
            self._libc.inotify_init1.restype = ctypes.c_int
            self._libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            self._libc.inotify_add_watch.restype = ctypes.c_int
            fd = self._libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if fd < 0:
                raise CapabilityUnavailableError("Linux inotify watcher unavailable")
            self._fd = fd
            try:
                # A singleton host-side flock prevents two controller instances
                # independently publishing the same next revision number.
                lock_fd = os.open(self.past_dir / ".monitor.lock",
                                  os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                                  0o600)
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    os.close(lock_fd)
                    raise StateMismatchError("another operator owns this AI history") from exc
                self._lock_fd = lock_fd
                # Read the persisted baseline. Refuse an offline change or an
                # orphaned receipt; neither can be silently explained as monitored.
                head = self.past_dir / "head.json"
                if head.exists():
                    if head.is_symlink() or not head.is_file():
                        raise ScopeDeniedError("AI history head is not a regular file")
                    previous = json.loads(head.read_text(encoding="utf-8"))
                    self._snapshot = previous["snapshot"]
                    self._sequence = previous["sequence"]
                    if (not isinstance(self._sequence, int) or self._sequence < 0
                            or not isinstance(self._snapshot, dict)
                            or previous["digest"] != _identity(self._snapshot)):
                        raise StateMismatchError("AI history baseline is corrupted")
                    if any(self.past_dir.glob("*.reverse.json")) and not (
                        self.past_dir / f"{self._sequence:012d}.reverse.json"
                    ).is_file():
                        raise StateMismatchError("AI history has an uncommitted reverse record")
                    if any(int(p.name[:12]) > self._sequence for p in self.past_dir.glob("*.reverse.json")):
                        raise StateMismatchError("AI history has an orphaned reversal")
                    # The current head alone is insufficient evidence: a stale
                    # head can be made self-consistent after an older revision
                    # or diff was corrupted. Replay the ENTIRE backwards chain
                    # under the same exclusive operator lock before admission.
                    from potbot.tools.ai_history_audit import audit_ai_history

                    audited = audit_ai_history(
                        self.past_dir, lock_held=True,
                        max_files=self.max_files,
                        max_snapshot_bytes=self.max_bytes,
                        max_document_bytes=max(64 * 1024 * 1024, self.max_bytes * 4),
                    )
                    if (audited.latest_sequence != self._sequence
                            or audited.current_digest != _identity(self._snapshot)):
                        raise StateMismatchError("AI configuration history chain failed readback")
                else:
                    if any(p.name != ".monitor.lock" for p in self.past_dir.iterdir()):
                        raise StateMismatchError("AI past has files without a trusted baseline")
                    self._snapshot = self._capture()
                self._add_watches()
                current = self._capture()
                if current != self._snapshot:
                    raise StateMismatchError("AI config changed while history watcher was offline")
                if not head.exists():
                    self._replace_head({"schema": "potbot-etc-ai-head/v1", "sequence": 0,
                                        "digest": _identity(self._snapshot), "snapshot": self._snapshot})
                self._fault = None
                self._started = True
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="potbot-etc-ai-watch", daemon=True)
                self._thread.start()
            except BaseException:
                os.close(fd)
                self._fd = None
                if self._lock_fd is not None:
                    os.close(self._lock_fd)
                    self._lock_fd = None
                self._fault = "failed start or unmonitored state"
                raise

    def _drain(self) -> list[dict[str, object]]:
        if self._fd is None:
            raise StateMismatchError("AI configuration inotify descriptor has closed")
        events: list[dict[str, object]] = []
        while True:
            try:
                data = os.read(self._fd, 65536)
            except BlockingIOError:
                break
            if not data:
                break
            offset = 0
            while offset < len(data):
                if len(data) - offset < _EVENT.size:
                    raise StateMismatchError("truncated Linux inotify event")
                wd, mask, cookie, length = _EVENT.unpack_from(data, offset)
                offset += _EVENT.size
                if len(data) - offset < length:
                    raise StateMismatchError("truncated inotify event filename")
                basename = data[offset:offset + length].split(b"\0", 1)[0]
                offset += length
                if mask & _IN_Q_OVERFLOW:
                    raise StateMismatchError("AI config watcher overflowed")
                parent = self._watch_dirs.get(wd)
                if parent is None:
                    raise StateMismatchError("inotify event for an unregistered watcher")
                if mask & _IN_IGNORED:
                    removed = self.config_dir if parent == "." else self.config_dir / parent
                    # Directory deletion normally invalidates its watch. Losing
                    # a watch for a still-existing directory is NOT normal.
                    if removed.exists():
                        raise StateMismatchError("AI config live directory lost its watcher")
                    del self._watch_dirs[wd]
                    continue
                try:
                    name = basename.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise StateMismatchError("AI config filename is not UTF-8") from exc
                relative = parent if not name else (name if parent == "." else f"{parent}/{name}")
                events.append({"path": relative, "mask": mask, "cookie": cookie,
                               "directory": bool(mask & _IN_ISDIR)})
                if len(events) > _MAX_EVENTS:
                    raise StateMismatchError("AI config event journal overflow")
        return events

    def poll_once(self) -> AiReverseRevision | None:
        """Drain kernel events AND scan, so persistent missed writes are detected."""
        with self._mutex:
            self.require_active()
            try:
                events = self._drain()
                self._add_watches()
                now = self._capture()
                changed = [p for p in sorted(set(self._snapshot) | set(now))
                           if self._snapshot.get(p) != now.get(p)]
                if not changed and not events:
                    return None
                seq = self._sequence + 1
                changes = [{"path": p, "before": self._snapshot.get(p), "after": now.get(p)}
                           for p in changed]
                before_digest, after_digest = _identity(self._snapshot), _identity(now)
                receipt = {"schema": "potbot-etc-ai-reversal/v1", "sequence": seq,
                           "from_sha256": after_digest, "restore_sha256": before_digest,
                           "changes": changes, "events": events,
                           "event_gaps_possible": True,
                           "note": "Inotify plus snapshots covers settled state; transient writes may be coalesced."}
                if reconstruct_prior_snapshot(now, receipt) != self._snapshot:
                    raise StateMismatchError("AI inverse state did not round-trip")
                inverse = self._write_once(f"{seq:012d}.reverse.json", _json_bytes(receipt))
                textual = self._write_once(f"{seq:012d}.reverse.diff",
                                           _render_reverse_diff(changes, seq).encode("utf-8"))
                self._sequence = seq
                self._replace_head({"schema": "potbot-etc-ai-head/v1", "sequence": seq,
                                    "digest": after_digest, "snapshot": now})
                self._snapshot = now
                return AiReverseRevision(seq, before_digest, after_digest, inverse, textual,
                                         len(events))
            except BaseException as exc:
                self._fault = type(exc).__name__ + ": " + str(exc)
                raise

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.poll_once()
            except BaseException:
                # All new launches fail closed. Never silently restart an
                # overflowed or potentially incomplete filesystem monitor.
                return

    def is_healthy(self) -> bool:
        """Nonblocking watch health for process cancellation predicates."""
        return (self._started and self._fd is not None and self._fault is None
                and not self._stop.is_set() and self._thread is not None
                and self._thread.is_alive())

    def require_active(self, *, config_dir: Path | None = None,
                       past_dir: Path | None = None) -> None:
        if not self.is_healthy():
            raise CapabilityUnavailableError("AI configuration change monitor is not active/healthy")
        if config_dir is not None and _physical_directory(config_dir) != self.config_dir:
            raise ScopeDeniedError("AI mount source differs from watched config directory")
        if past_dir is not None and _physical_directory(past_dir) != self.past_dir:
            raise ScopeDeniedError("AI history mount differs from watched history directory")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and threading.current_thread() is not self._thread:
            self._thread.join(timeout=5)
        with self._mutex:
            self._fault = self._fault or "operator stopped"
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None

    def __enter__(self) -> AiConfigHistoryMonitor:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()
