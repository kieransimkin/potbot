"""Operator-only Linux taskstats exit collector for a monitored worker lifetime.

Registered on *all online CPUs before* the worker is started, it receives a
kernel TASKSTATS_TYPE_AGGR_PID event for each exiting task (including threads and
short-lived children). The caller joins reports to syscall-trace PIDs after the
process has stopped; unrelated kernel events are never written to disk or sent to
clients. Overflow, inaccessible taskstats, missing events, or PID reuse make the
worker audit indeterminate, never 'complete'.

Requires CONFIG_TASKSTATS, CONFIG_TASK_XACCT and CONFIG_TASK_IO_ACCOUNTING,
and privileges to register TASKSTATS_CMD_ATTR_REGISTER_CPUMASK. Do not use a
polling approximation as an audit-success fallback. Does not capture GPU use.
"""
from __future__ import annotations

import ctypes
import os
import re
import select
import socket
import struct
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from potbot.domain.errors import CapabilityUnavailableError

# UAPI: linux/netlink.h, linux/genetlink.h, linux/taskstats.h (64-bit Linux).
_NLMSG = struct.Struct("=IHHII")
_GENL = struct.Struct("=BBH")
_NLA = struct.Struct("=HH")
_NLM_F_REQUEST = 1
_NLM_F_ACK = 4
_NLMSG_ERROR = 2
_NLMSG_OVERRUN = 4
_GENL_ID_CTRL = 0x10
_CTRL_CMD_GETFAMILY = 3
_CTRL_ATTR_FAMILY_ID = 1
_CTRL_ATTR_FAMILY_NAME = 2
_TASKSTATS_CMD_GET = 1
_TASKSTATS_TYPE_PID = 1
_TASKSTATS_TYPE_STATS = 3
_TASKSTATS_TYPE_AGGR_PID = 4
_TASKSTATS_CMD_ATTR_REGISTER_CPUMASK = 3
_TASKSTATS_CMD_ATTR_DEREGISTER_CPUMASK = 4
_ATTR_MASK = 0x3FFF
_MAX_PACKETS = 100_000
_MAX_DATAGRAM = 1 << 20


class _Taskstats(ctypes.Structure):
    """Field offsets are checked against Linux UAPI in qualification tests."""
    _fields_ = [
        ("version", ctypes.c_uint16), ("ac_exitcode", ctypes.c_uint32),
        ("ac_flag", ctypes.c_uint8), ("ac_nice", ctypes.c_uint8),
        ("cpu_count", ctypes.c_uint64), ("cpu_delay_total", ctypes.c_uint64),
        ("blkio_count", ctypes.c_uint64), ("blkio_delay_total", ctypes.c_uint64),
        ("swapin_count", ctypes.c_uint64), ("swapin_delay_total", ctypes.c_uint64),
        ("cpu_run_real_total", ctypes.c_uint64), ("cpu_run_virtual_total", ctypes.c_uint64),
        ("ac_comm", ctypes.c_char * 32), ("ac_sched", ctypes.c_uint8),
        ("ac_pad", ctypes.c_uint8 * 3), ("_uid_alignment", ctypes.c_uint32),
        ("ac_uid", ctypes.c_uint32),
        ("ac_gid", ctypes.c_uint32), ("ac_pid", ctypes.c_uint32),
        ("ac_ppid", ctypes.c_uint32), ("ac_btime", ctypes.c_uint32),
        ("ac_etime", ctypes.c_uint64), ("ac_utime", ctypes.c_uint64),
        ("ac_stime", ctypes.c_uint64), ("ac_minflt", ctypes.c_uint64),
        ("ac_majflt", ctypes.c_uint64), ("coremem", ctypes.c_uint64),
        ("virtmem", ctypes.c_uint64), ("hiwater_rss", ctypes.c_uint64),
        ("hiwater_vm", ctypes.c_uint64), ("read_char", ctypes.c_uint64),
        ("write_char", ctypes.c_uint64), ("read_syscalls", ctypes.c_uint64),
        ("write_syscalls", ctypes.c_uint64), ("read_bytes", ctypes.c_uint64),
        ("write_bytes", ctypes.c_uint64), ("cancelled_write_bytes", ctypes.c_uint64),
        ("nvcsw", ctypes.c_uint64), ("nivcsw", ctypes.c_uint64),
        ("ac_utimescaled", ctypes.c_uint64), ("ac_stimescaled", ctypes.c_uint64),
        ("cpu_scaled_run_real_total", ctypes.c_uint64),
        ("freepages_count", ctypes.c_uint64), ("freepages_delay_total", ctypes.c_uint64),
        ("thrashing_count", ctypes.c_uint64), ("thrashing_delay_total", ctypes.c_uint64),
        ("ac_btime64", ctypes.c_uint64), ("compact_count", ctypes.c_uint64),
        ("compact_delay_total", ctypes.c_uint64), ("ac_tgid", ctypes.c_uint32),
        ("ac_tgetime", ctypes.c_uint64), ("ac_exe_dev", ctypes.c_uint64),
        ("ac_exe_inode", ctypes.c_uint64), ("wpcopy_count", ctypes.c_uint64),
        ("wpcopy_delay_total", ctypes.c_uint64), ("irq_count", ctypes.c_uint64),
        ("irq_delay_total", ctypes.c_uint64),
    ]

# Reject an architecture/layout mismatch before ever parsing counters.
_EXPECTED_LAYOUT = {
    "version": 0, "ac_pid": 128, "ac_ppid": 132, "ac_btime": 136,
    "ac_etime": 144, "ac_utime": 152, "ac_stime": 160,
    "hiwater_rss": 200, "read_bytes": 248, "write_bytes": 256,
    "ac_btime64": 344, "ac_tgid": 368,
}


def check_taskstats_layout() -> None:
    if ctypes.sizeof(_Taskstats) < 408 or any(
        getattr(_Taskstats, name).offset != offset
        for name, offset in _EXPECTED_LAYOUT.items()
    ):
        raise CapabilityUnavailableError("taskstats kernel UAPI layout not qualified")


def _align(n: int) -> int:
    return (n + 3) & ~3


def _attrs(payload: bytes) -> Iterator[tuple[int, bytes]]:
    position = 0
    while position < len(payload):
        if position + _NLA.size > len(payload):
            raise CapabilityUnavailableError("truncated taskstats attribute")
        length, kind = _NLA.unpack_from(payload, position)
        if length < _NLA.size or position + length > len(payload):
            raise CapabilityUnavailableError("invalid taskstats attribute length")
        yield kind & _ATTR_MASK, payload[position + _NLA.size:position + length]
        position += _align(length)
    if position != len(payload):
        raise CapabilityUnavailableError("misaligned taskstats attributes")


def _pack_attr(kind: int, value: bytes) -> bytes:
    length = _NLA.size + len(value)
    return _NLA.pack(length, kind) + value + b"\0" * (_align(length) - length)


@dataclass(frozen=True, slots=True)
class ExitAccounting:
    pid: int
    tgid: int
    ppid: int
    started_seconds: int
    elapsed_usec: int
    cpu_user_usec: int
    cpu_system_usec: int
    rss_peak_bytes: int
    block_read_bytes: int
    block_write_bytes: int
    io_read_char_bytes: int
    io_write_char_bytes: int
    minor_faults: int
    major_faults: int
    taskstats_version: int

    def public_private_record(self) -> dict[str, int]:
        # Operator-private resource amounts only, no process names/argv/UID.
        return {
            "pid": self.pid, "tgid": self.tgid, "ppid": self.ppid,
            "started_seconds": self.started_seconds,
            "elapsed_usec": self.elapsed_usec, "cpu_user_usec": self.cpu_user_usec,
            "cpu_system_usec": self.cpu_system_usec,
            "cpu_total_usec": self.cpu_user_usec + self.cpu_system_usec,
            "rss_peak_bytes": self.rss_peak_bytes,
            "block_read_bytes": self.block_read_bytes,
            "block_write_bytes": self.block_write_bytes,
            "io_read_char_bytes": self.io_read_char_bytes,
            "io_write_char_bytes": self.io_write_char_bytes,
            "minor_faults": self.minor_faults, "major_faults": self.major_faults,
            "taskstats_version": self.taskstats_version,
        }


def _decode_exit(payload: bytes) -> ExitAccounting:
    check_taskstats_layout()
    if len(payload) < _Taskstats.write_bytes.offset + 8:
        raise CapabilityUnavailableError("kernel taskstats lacks exit I/O fields")
    # v10 adds a 64-bit birth time; require it to constrain PID reuse.
    if len(payload) < _Taskstats.ac_btime64.offset + 8:
        raise CapabilityUnavailableError("kernel taskstats lacks 64-bit birth time")
    stat = _Taskstats.from_buffer_copy(payload[:ctypes.sizeof(_Taskstats)].ljust(ctypes.sizeof(_Taskstats), b"\0"))
    if not 10 <= stat.version <= 255 or stat.ac_pid == 0 or stat.ac_btime64 == 0:
        raise CapabilityUnavailableError("taskstats version or task identity is unsupported")
    return ExitAccounting(
        pid=stat.ac_pid, tgid=stat.ac_tgid or stat.ac_pid,
        ppid=stat.ac_ppid, started_seconds=stat.ac_btime64,
        elapsed_usec=stat.ac_etime, cpu_user_usec=stat.ac_utime,
        cpu_system_usec=stat.ac_stime, rss_peak_bytes=stat.hiwater_rss * 1024,
        block_read_bytes=stat.read_bytes, block_write_bytes=stat.write_bytes,
        io_read_char_bytes=stat.read_char, io_write_char_bytes=stat.write_char,
        minor_faults=stat.ac_minflt, major_faults=stat.ac_majflt,
        taskstats_version=stat.version,
    )


def _netlink_messages(raw: bytes) -> Iterator[tuple[int, int, int, int, bytes]]:
    pos = 0
    while pos < len(raw):
        if pos + _NLMSG.size > len(raw):
            raise CapabilityUnavailableError("truncated kernel netlink datagram")
        length, kind, flags, sequence, sender = _NLMSG.unpack_from(raw, pos)
        if length < _NLMSG.size or pos + length > len(raw):
            raise CapabilityUnavailableError("invalid kernel netlink message")
        yield kind, flags, sequence, sender, raw[pos + _NLMSG.size:pos + length]
        pos += _align(length)
    if pos != len(raw):
        raise CapabilityUnavailableError("misaligned netlink datagram")


def _taskstats_records(datagram: bytes, family: int) -> list[ExitAccounting]:
    result: list[ExitAccounting] = []
    for kind, _flags, _sequence, _sender, payload in _netlink_messages(datagram):
        if kind == _NLMSG_OVERRUN:
            raise CapabilityUnavailableError("kernel taskstats exit stream overflow")
        if kind == _NLMSG_ERROR:
            if len(payload) < 4 or struct.unpack_from("=i", payload)[0] < 0:
                raise CapabilityUnavailableError("kernel taskstats socket returned an error")
            continue
        if kind != family:
            continue
        if len(payload) < _GENL.size:
            raise CapabilityUnavailableError("truncated taskstats GENL header")
        for kind, value in _attrs(payload[_GENL.size:]):
            if kind != _TASKSTATS_TYPE_AGGR_PID:
                continue  # AGGR_TGID is a second total; never double count it.
            values = dict(_attrs(value))
            pid_raw, stats_raw = values.get(_TASKSTATS_TYPE_PID), values.get(_TASKSTATS_TYPE_STATS)
            if pid_raw is None or len(pid_raw) < 4 or stats_raw is None:
                raise CapabilityUnavailableError("incomplete taskstats process-exit event")
            stat = _decode_exit(stats_raw)
            if stat.pid != struct.unpack_from("=I", pid_raw)[0]:
                raise CapabilityUnavailableError("taskstats envelope PID mismatch")
            result.append(stat)
    return result


def _online_cpus() -> str:
    text = Path("/sys/devices/system/cpu/online").read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9]{1,5}(?:-[0-9]{1,5})?(?:,[0-9]{1,5}(?:-[0-9]{1,5})?)*", text) or len(text) > 2048:
        raise CapabilityUnavailableError("kernel online CPU mask unavailable")
    # Register on the whole machine, not just the host operator affinity mask.
    return text


class TaskstatsExitStream:
    """Collect per-task kernel exit totals; MUST open before spawning workers."""
    def __init__(self, *, max_records: int = _MAX_PACKETS) -> None:
        if not 1 <= max_records <= _MAX_PACKETS:
            raise ValueError("taskstats event budget invalid")
        check_taskstats_layout()
        self._max_records = max_records
        self._start_wall = time.time()
        self._records: dict[int, list[ExitAccounting]] = {}
        self._failure: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._cpus: str | None = None
        self._family = 0
        self._sequence = 0
        self._registered = False
        self._lock = threading.Lock()
        self._record_count = 0

    @property
    def healthy(self) -> bool:
        with self._lock:
            return self._failure is None and self._registered and self._thread is not None and self._thread.is_alive()

    def _next(self) -> int:
        self._sequence += 1
        return self._sequence

    def _send(self, kind: int, cmd: int, attrs: bytes, *, acknowledge: bool = False) -> int:
        assert self._sock is not None
        sequence = self._next()
        data = _GENL.pack(cmd, 1, 0) + attrs
        packet = _NLMSG.pack(_NLMSG.size + len(data), kind,
                             _NLM_F_REQUEST | (_NLM_F_ACK if acknowledge else 0),
                             sequence, 0) + data
        self._sock.sendto(packet, (0, 0))
        return sequence

    def _recv_init(self, sequence: int) -> bytes:
        assert self._sock is not None
        while True:
            raw, _anc, flags, sender = self._sock.recvmsg(_MAX_DATAGRAM)
            if flags & socket.MSG_TRUNC or sender[0] != 0:
                raise CapabilityUnavailableError("taskstats initialization reply truncated or untrusted")
            for kind, _flags, seq, _sender, payload in _netlink_messages(raw):
                if seq != sequence:
                    raise CapabilityUnavailableError("unexpected event before taskstats registration completed")
                if kind == _NLMSG_ERROR:
                    if len(payload) < 4:
                        raise CapabilityUnavailableError("truncated taskstats registration acknowledgement")
                    code = struct.unpack_from("=i", payload)[0]
                    if code < 0:
                        raise OSError(-code, os.strerror(-code))
                    return b""
                return _NLMSG.pack(_NLMSG.size + len(payload), kind, 0, seq, 0) + payload

    def open(self) -> None:
        if self._sock is not None:
            raise CapabilityUnavailableError("taskstats collector already initialized")
        if os.name != "posix" or not hasattr(socket, "AF_NETLINK"):
            raise CapabilityUnavailableError("kernel taskstats requires Linux generic netlink")
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 16)  # NETLINK_GENERIC from linux/netlink.h
            sock.bind((0, 0))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _MAX_DATAGRAM * 8)
            sock.settimeout(2.0)
            self._sock = sock
            family_seq = self._send(_GENL_ID_CTRL, _CTRL_CMD_GETFAMILY,
                _pack_attr(_CTRL_ATTR_FAMILY_NAME, b"TASKSTATS\0"))
            raw = self._recv_init(family_seq)
            family = 0
            for kind, _flags, _seq, _sender, payload in _netlink_messages(raw):
                if kind != _GENL_ID_CTRL or len(payload) < _GENL.size:
                    raise CapabilityUnavailableError("taskstats generic netlink family missing")
                for attr, value in _attrs(payload[_GENL.size:]):
                    if attr == _CTRL_ATTR_FAMILY_ID and len(value) >= 2:
                        family = struct.unpack_from("=H", value)[0]
            if family < 0x10:
                raise CapabilityUnavailableError("kernel TASKSTATS family unavailable")
            self._family = family
            self._cpus = _online_cpus()
            seq = self._send(family, _TASKSTATS_CMD_GET,
                _pack_attr(_TASKSTATS_CMD_ATTR_REGISTER_CPUMASK, self._cpus.encode() + b"\0"),
                acknowledge=True)
            self._recv_init(seq)
            self._registered = True
            sock.settimeout(None)
            self._thread = threading.Thread(target=self._read, name="potbot-taskstats-exit", daemon=True)
            self._thread.start()
        except (OSError, ValueError, CapabilityUnavailableError) as exc:
            if sock:
                sock.close()
            self._sock = None
            self._registered = False
            raise CapabilityUnavailableError("kernel taskstats exit collection unavailable") from exc

    def _read(self) -> None:
        try:
            while not self._stop.is_set():
                assert self._sock is not None
                readable, _, _ = select.select([self._sock], [], [], 0.05)
                if not readable:
                    continue
                raw, _anc, flags, sender = self._sock.recvmsg(_MAX_DATAGRAM)
                if flags & socket.MSG_TRUNC or sender[0] != 0:
                    raise CapabilityUnavailableError("taskstats exit stream truncated or untrusted")
                entries = _taskstats_records(raw, self._family)
                with self._lock:
                    for entry in entries:
                        if self._record_count >= self._max_records:
                            raise CapabilityUnavailableError("taskstats exited-task budget exhausted")
                        self._record_count += 1
                        self._records.setdefault(entry.pid, []).append(entry)
        except BaseException as exc:
            # Worker threads are operator infrastructure, not task callbacks:
            # unexpected parsing/runtime faults must NEVER be interpreted as a
            # complete audit simply because packets were already collected.
            with self._lock:
                self._failure = type(exc).__name__
        finally:
            if not self._stop.is_set():
                with self._lock:
                    self._failure = self._failure or "listener_exited_unexpectedly"

    def coverage(self, traced_pids: set[int], *, deadline_seconds: float = 2.0) -> dict[int, ExitAccounting]:
        """Wait for exit events, reject missing/duplicate or reused task identifiers."""
        if not traced_pids or any(type(pid) is not int or not 0 < pid < 2**31 for pid in traced_pids):
            raise CapabilityUnavailableError("trace PID set invalid for kernel accounting")
        stop_at = time.monotonic() + deadline_seconds
        while True:
            with self._lock:
                if (self._failure or not self._registered or
                        self._thread is None or not self._thread.is_alive()):
                    raise CapabilityUnavailableError("kernel taskstats exit channel unavailable")
                groups = {pid: self._records.get(pid, []) for pid in traced_pids}
                if all(groups.values()):
                    break
            if time.monotonic() >= stop_at:
                raise CapabilityUnavailableError("kernel taskstats missing traced task exits")
            time.sleep(0.01)
        result: dict[int, ExitAccounting] = {}
        for pid, entries in groups.items():
            # A real reused PID within a worker lifetime is ambiguous; do not merge it.
            if len(entries) != 1:
                raise CapabilityUnavailableError("PID reuse or duplicate taskstats event")
            record = entries[0]
            # Reject an old process exiting during the trace with a recycled PID.
            if record.started_seconds < int(self._start_wall) - 2:
                raise CapabilityUnavailableError("PID birth time predates worker taskstats registration")
            result[pid] = record
        return result

    def close(self) -> None:
        # Distinguish intentional stop from a receiver that died early.
        thread = self._thread
        died_before_stop = thread is not None and not thread.is_alive()
        self._stop.set()
        if thread is not None:
            thread.join(timeout=2)
        still_alive = thread is not None and thread.is_alive()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                if self._registered and self._cpus:
                    self._sock = sock
                    self._send(self._family, _TASKSTATS_CMD_GET,
                        _pack_attr(_TASKSTATS_CMD_ATTR_DEREGISTER_CPUMASK, self._cpus.encode() + b"\0"))
            except OSError:
                pass
            finally:
                sock.close()
                self._sock = None
        self._registered = False
        if died_before_stop or still_alive or self._failure:
            raise CapabilityUnavailableError("taskstats exit listener did not shut down cleanly")
