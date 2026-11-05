"""One-run TASKSTATS channel for an administrator-created worker namespace.

The launcher owns both ends of a Unix socketpair. The auditor and the worker
monitor share the worker PID namespace, and the launcher binds both ends to
one preassigned cgroup leaf. Only accounting receipts cross this channel.
"""

from __future__ import annotations

import json
import os
import re
import select
import socket
import struct
import threading
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.domain.errors import CapabilityUnavailableError
from potbot.tools.taskstats_exit import ExitAccounting, TaskstatsExitStream

_PROFILE = "potbot.scoped-taskstats/1"
_PACKET_LIMIT = 128 * 1024
_BATCH_LIMIT = 128
_RECORD_LIMIT = 100_000
_FIELDS = {field.name for field in fields(ExitAccounting)}


def _trusted_pair(channel: socket.socket) -> None:
    # SO_PEERCRED on a socketpair reports the credentials at creation.
    if (
        channel.family != socket.AF_UNIX
        or channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
        or channel.getsockname()
        or channel.getpeername()
        or struct.unpack("iII", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
        != 0
    ):
        raise CapabilityUnavailableError(
            "Scoped TASKSTATS requires an administrator-owned socketpair"
        )


def _send(channel: socket.socket, value: dict[str, Any]) -> None:
    data = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("ascii")
    if len(data) > _PACKET_LIMIT or channel.send(data) != len(data):
        raise CapabilityUnavailableError("Scoped TASKSTATS packet exceeded its bound")


def _receive(channel: socket.socket) -> dict[str, Any]:
    data, _, flags, _ = channel.recvmsg(_PACKET_LIMIT)
    if not data or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
        raise CapabilityUnavailableError("Scoped TASKSTATS channel closed or truncated")
    value = json.loads(data)
    if type(value) is not dict:
        raise CapabilityUnavailableError("Invalid scoped TASKSTATS message")
    return value


def _cgroup_identity(cgroup: Path) -> list[int]:
    if not cgroup.is_absolute() or cgroup.resolve(strict=True) != cgroup:
        raise CapabilityUnavailableError("Scoped TASKSTATS requires a canonical cgroup leaf")
    descriptor = os.open(cgroup, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        with os.fdopen(
            os.open("cgroup.events", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor), "rb"
        ) as events:
            data = events.read(4097)
        info = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(data) > 4096 or [
        line for line in data.splitlines() if line.startswith(b"populated ")
    ] != [b"populated 0"]:
        raise CapabilityUnavailableError("Scoped TASKSTATS requires an empty worker cgroup")
    return [info.st_dev, info.st_ino]


def _pids(value: object, *, limit: int) -> list[int]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= limit
        or any(type(pid) is not int or not 0 < pid < 2**31 for pid in value)
        or len(value) != len(set(value))
    ):
        raise CapabilityUnavailableError("Invalid scoped TASKSTATS process set")
    return value


def _binding(worker_id: UUID) -> dict[str, str]:
    return {
        "profile": _PROFILE,
        "worker_id": str(worker_id),
        "pid_namespace": os.readlink("/proc/self/ns/pid"),
    }


class ScopedTaskstatsClient:
    """One-shot collector supplied by a trusted namespace launcher."""

    def __init__(
        self, channel: socket.socket, worker_id: UUID, *, pid_namespace: str | None = None
    ) -> None:
        self.worker_id = worker_id
        self._channel = channel
        self._lock = threading.Lock()
        self._opened = self._closed = False
        try:
            _trusted_pair(channel)
            self._binding = _binding(worker_id)
            if pid_namespace is not None:
                if not re.fullmatch(r"pid:\[\d+\]", pid_namespace):
                    raise CapabilityUnavailableError("Invalid scoped TASKSTATS PID namespace")
                self._binding["pid_namespace"] = pid_namespace
            channel.settimeout(4)
        except BaseException:
            channel.close()
            raise

    def _exchange(self, op: str, **values: Any) -> dict[str, Any]:
        try:
            _send(self._channel, {**self._binding, "op": op, **values})
            answer = _receive(self._channel)
            if answer.pop("op", None) != op or any(
                answer.pop(key, None) != value for key, value in self._binding.items()
            ):
                raise CapabilityUnavailableError("Scoped TASKSTATS receipt does not match its run")
            return answer
        except (OSError, ValueError) as exc:
            raise CapabilityUnavailableError("Scoped TASKSTATS exchange failed") from exc

    def open(self, cgroup: Path) -> None:
        with self._lock:
            if self._opened or self._closed:
                raise CapabilityUnavailableError("Scoped TASKSTATS channel already consumed")
            try:
                if cgroup.name != self.worker_id.hex:
                    raise CapabilityUnavailableError(
                        "Scoped TASKSTATS cgroup does not match its run"
                    )
                identity = _cgroup_identity(cgroup)
                if self._exchange("open", cgroup=identity) != {"cgroup": identity}:
                    raise CapabilityUnavailableError(
                        "Scoped TASKSTATS did not acknowledge its cgroup"
                    )
                self._opened = True
            except BaseException:
                self._closed = True
                self._channel.close()
                raise

    @property
    def healthy(self) -> bool:
        with self._lock:
            if not self._opened or self._closed:
                return False
            poller = select.poll()
            poller.register(self._channel, select.POLLIN | select.POLLHUP | select.POLLERR)
            # The protocol has no unsolicited packets. Readability while idle is a fault or EOF.
            return not poller.poll(0)

    def coverage(self, traced_pids: set[int]) -> dict[int, ExitAccounting]:
        pids = sorted(_pids(list(traced_pids), limit=_RECORD_LIMIT))
        records: dict[int, ExitAccounting] = {}
        with self._lock:
            if not self._opened or self._closed:
                raise CapabilityUnavailableError("Scoped TASKSTATS channel is unavailable")
            for start in range(0, len(pids), _BATCH_LIMIT):
                batch = pids[start : start + _BATCH_LIMIT]
                answer = self._exchange("coverage", pids=batch)
                rows = answer.get("records")
                if set(answer) != {"records"} or type(rows) is not list or len(rows) != len(batch):
                    raise CapabilityUnavailableError("Scoped TASKSTATS coverage is incomplete")
                for pid, row in zip(batch, rows, strict=True):
                    if (
                        type(row) is not dict
                        or row.keys() != _FIELDS
                        or any(
                            type(value) is not int or not 0 <= value < 2**64
                            for value in row.values()
                        )
                        or row["pid"] != pid
                        or not 10 <= row["taskstats_version"] <= 255
                    ):
                        raise CapabilityUnavailableError("Invalid scoped TASKSTATS record")
                    records[pid] = ExitAccounting(**row)
        return records

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                if self._opened and self._exchange("close"):
                    raise CapabilityUnavailableError("Scoped TASKSTATS did not confirm shutdown")
            finally:
                self._closed = True
                self._channel.close()


def serve_scoped_taskstats(
    channel: socket.socket,
    worker_id: UUID,
    cgroup: Path,
    controller_pid: int | None,
    *,
    parent_namespaces: tuple[str, str],
) -> None:
    """Serve the fixed scope handed down by the administrator's launcher."""
    binding = _binding(worker_id)
    stream = TaskstatsExitStream()
    closed = False
    try:
        _trusted_pair(channel)
        namespaces = (binding["pid_namespace"], os.readlink("/proc/self/ns/net"))
        status = dict(
            line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines()
        )
        if (
            os.geteuid() == 0
            or int(status["CapEff"], 16) != 1 << 12  # CAP_NET_ADMIN
            or status["NoNewPrivs"].strip() != "1"
            or any(
                current == parent
                for current, parent in zip(namespaces, parent_namespaces, strict=True)
            )
        ):
            raise CapabilityUnavailableError("Scoped TASKSTATS auditor isolation is unavailable")
        if cgroup.name != worker_id.hex or (
            controller_pid is not None and not 1 < controller_pid < 2**31
        ):
            raise CapabilityUnavailableError("Invalid scoped TASKSTATS launch binding")
        channel.settimeout(15)
        request = _receive(channel)
        identity = _cgroup_identity(cgroup)
        if request != {**binding, "op": "open", "cgroup": identity}:
            raise CapabilityUnavailableError("Scoped TASKSTATS open request does not match its run")
        stream.open()
        if not stream.healthy:
            raise CapabilityUnavailableError("Scoped TASKSTATS registration did not stay live")
        channel.settimeout(4)
        _send(channel, {**binding, "op": "open", "cgroup": identity})
        protected = {1, os.getpid()}
        if controller_pid is not None:
            protected.add(controller_pid)
        requested: set[int] = set()
        while stream.healthy:
            if not select.select([channel], [], [], 0.1)[0]:
                continue
            request = _receive(channel)
            if _cgroup_identity(cgroup) != identity:
                raise CapabilityUnavailableError("Scoped TASKSTATS cgroup changed during the run")
            if request == {**binding, "op": "close"}:
                closed = True
                stream.close()
                _send(channel, {**binding, "op": "close"})
                return
            pids = _pids(request.get("pids"), limit=_BATCH_LIMIT)
            if (
                request != {**binding, "op": "coverage", "pids": pids}
                or protected.intersection(pids)
                or requested.intersection(pids)
                or len(requested) + len(pids) > _RECORD_LIMIT
            ):
                raise CapabilityUnavailableError("Scoped TASKSTATS process request escaped its run")
            requested.update(pids)
            records = stream.coverage(set(pids))
            _send(
                channel,
                {**binding, "op": "coverage", "records": [asdict(records[pid]) for pid in pids]},
            )
        raise CapabilityUnavailableError("Scoped TASKSTATS listener lost during worker run")
    finally:
        channel.close()
        if not closed:
            stream.close()
