"""Socket-activated Linux worker launcher. The user service holds no capabilities."""

from __future__ import annotations

import hashlib
import json
import os
import select
import socket
import stat
import struct
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.domain.errors import CapabilityUnavailableError
from potbot.tools.scoped_taskstats import (
    ScopedTaskstatsClient,
    _cgroup_identity,
    serve_scoped_taskstats,
)

DEFAULT_SOCKET = Path("/run/potbot-scoped-worker.sock")
WSL_DRIVER_SHARE = "/usr/lib/wsl/drivers"
_MODULE = "potbot.tools.scoped_launcher"
_PACKET = 128 * 1024
_MAX_FDS = 8
_SAFE_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


def _peer(channel: socket.socket) -> tuple[int, int, int]:
    return struct.unpack("iII", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))


def _send(channel: socket.socket, request: dict[str, Any], fds: tuple[int, ...] = ()) -> None:
    data = json.dumps(request, ensure_ascii=True, allow_nan=False).encode("ascii")
    if len(data) > _PACKET or socket.send_fds(channel, [data], fds) != len(data):
        raise CapabilityUnavailableError("Scoped launcher request exceeded its bound")


def _receive(
    channel: socket.socket, resources: ExitStack, max_fds: int = 0
) -> tuple[dict[str, Any], list[int]]:
    data, fds, flags, _ = socket.recv_fds(channel, _PACKET, max_fds, socket.MSG_CMSG_CLOEXEC)
    for fd in fds:
        resources.callback(os.close, fd)
    if not data or flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC):
        raise CapabilityUnavailableError("Scoped launcher channel closed or truncated")
    value = json.loads(data)
    if type(value) is not dict:
        raise CapabilityUnavailableError("Invalid scoped launcher message")
    return value, fds


def check_endpoint(path: Path) -> None:
    try:
        info = path.lstat()
        parent = path.parent.stat()
        if (
            not path.is_absolute()
            or path.resolve(strict=True) != path
            or not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != 0
            or parent.st_uid != 0
            or parent.st_mode & 0o022
        ):
            raise ValueError
    except (OSError, ValueError) as exc:
        raise CapabilityUnavailableError(
            "Administrator-owned scoped worker launcher unavailable"
        ) from exc


class ScopedWorkerLaunch:
    """Allocate a per-run auditor and carry the sandbox launch over its control socket."""

    def __init__(self, endpoint: Path, worker_id: UUID, *, probe: bool = False) -> None:
        check_endpoint(endpoint)
        self.channel = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            self.channel.settimeout(8)
            self.channel.connect(str(endpoint))
            if _peer(self.channel)[1] != 0:
                raise CapabilityUnavailableError("Scoped worker launcher has an untrusted owner")
            request: dict[str, Any] = {"worker_id": str(worker_id)}
            if probe:
                request["probe"] = True
            _send(self.channel, request)
            with ExitStack() as resources:
                response, fds = _receive(self.channel, resources, 1)
                if set(response) != {"pid_namespace"} or len(fds) != 1:
                    raise CapabilityUnavailableError(
                        "Scoped worker launcher did not allocate a run"
                    )
                self.collector = ScopedTaskstatsClient(
                    socket.socket(fileno=os.dup(fds[0])),
                    worker_id,
                    pid_namespace=response["pid_namespace"],
                )
        except (OSError, ValueError, TypeError) as exc:
            self.channel.close()
            raise CapabilityUnavailableError("Scoped worker launch allocation failed") from exc
        except BaseException:
            self.channel.close()
            raise

    def command(self, argv: tuple[str, ...], pass_fds: tuple[int, ...]) -> tuple[str, ...]:
        return (
            sys.executable,
            "-I",
            "-m",
            _MODULE,
            "proxy",
            str(self.channel.fileno()),
            ",".join(map(str, pass_fds)),
            *argv,
        )

    def close(self) -> None:
        self.channel.close()

    def confirm_probe(self, cgroup: Path, join_sha256: str) -> None:
        self.collector.open(cgroup)
        if not self.collector.healthy:
            raise CapabilityUnavailableError("Scoped TASKSTATS probe did not stay live")
        self.collector.close()
        try:
            _send(self.channel, {"probe": "complete"})
            with ExitStack() as resources:
                response, _ = _receive(self.channel, resources)
            if response != {"probe": "complete", "join_sha256": join_sha256}:
                raise CapabilityUnavailableError("Scoped worker probe did not verify its helper")
        except (OSError, ValueError) as exc:
            raise CapabilityUnavailableError(
                "Scoped worker probe could not confirm shutdown"
            ) from exc


def _proxy(fd: int, targets: list[int], argv: list[str]) -> int:
    with socket.socket(fileno=fd) as channel, ExitStack() as resources:
        channel.setblocking(True)
        # The monitor authenticates the broker before handing this connection down.
        if _peer(channel)[1] != 0:
            return 125
        _send(
            channel,
            {"argv": argv, "env": dict(os.environ), "targets": targets},
            (0, 1, 2, *targets),
        )
        # Only the scoped child should keep the worker's stdio and status pipe open.
        for descriptor in (0, 1, 2, *targets):
            os.close(descriptor)
        response, _ = _receive(channel, resources)
        code = response.get("exit")
        if set(response) != {"exit"} or type(code) is not int or not -64 <= code <= 255:
            return 125
        return code if code >= 0 else 128 - code


def _credentials(uid: int, gid: int, capability: str = "-all") -> list[str]:
    return [
        "/usr/bin/setpriv",
        f"--reuid={uid}",
        f"--regid={gid}",
        "--clear-groups",
        "--no-new-privs",
        f"--bounding-set={capability}",
        f"--inh-caps={capability}",
        f"--ambient-caps={capability}",
        "--",
    ]


def _worker_exec(spec_fd: int, received: list[int]) -> None:
    import fcntl

    if os.geteuid() == 0:
        raise CapabilityUnavailableError("Scoped worker must run under its controller UID")
    with os.fdopen(spec_fd, "rb") as reader:
        spec = json.load(reader)
    targets = spec["targets"]
    copies = [
        fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, max(targets, default=2) + 1) for fd in received
    ]
    for fd in received:
        os.close(fd)
    for source, target in zip(copies, targets, strict=True):
        os.dup2(source, target)
        os.close(source)
    os.execve(spec["argv"][0], spec["argv"], spec["env"])


def _validate_worker(request: dict[str, Any], fds: list[int]) -> list[int]:
    if set(request) != {"argv", "env", "targets"}:
        raise CapabilityUnavailableError("Invalid scoped worker launch")
    argv, env, targets = (request[key] for key in ("argv", "env", "targets"))
    if (
        type(argv) is not list
        or not argv
        or not all(isinstance(arg, str) for arg in argv)
        or not os.path.isabs(argv[0])
        or type(env) is not dict
        or not all(isinstance(value, str) for value in env.values())
        or type(targets) is not list
        or len(targets) > _MAX_FDS
        or any(type(fd) is not int or not 3 <= fd < 65536 for fd in targets)
        or len(set(targets)) != len(targets)
        or len(fds) != len(targets) + 3
    ):
        raise CapabilityUnavailableError("Invalid scoped worker command or descriptors")
    return targets


def _scope(
    channel: socket.socket,
    leaf: Path,
    uid: int,
    gid: int,
    auditor_uid: int,
    join: Path,
    parent_namespaces: tuple[str, str],
    *,
    probe: bool = False,
) -> None:
    if os.getpid() != 1 or os.geteuid() != 0:
        raise CapabilityUnavailableError("Scoped launcher requires a private PID namespace")
    mounts = Path("/proc/self/mountinfo")
    if any(
        row.split()[4] == WSL_DRIVER_SHARE and " - 9p " in row
        for row in mounts.read_text().splitlines()
    ):
        subprocess.run(["/usr/bin/umount", "--", WSL_DRIVER_SHARE], check=True)
    if any(" - 9p " in row or " - drvfs " in row for row in mounts.read_text().splitlines()):
        raise CapabilityUnavailableError("Windows filesystems are visible inside the worker scope")
    identity = _cgroup_identity(leaf)
    worker: subprocess.Popen[bytes] | None = None
    auditor: subprocess.Popen[bytes] | None = None
    with ExitStack() as resources:
        first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        resources.enter_context(first)
        resources.enter_context(second)
        cgroup_fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        resources.callback(os.close, cgroup_fd)
        info = os.fstat(cgroup_fd)
        if identity != [info.st_dev, info.st_ino]:
            raise CapabilityUnavailableError("Scoped worker cgroup changed")
        try:
            auditor = subprocess.Popen(
                [
                    *_credentials(auditor_uid, auditor_uid, "-all,+net_admin"),
                    sys.executable,
                    "-I",
                    "-m",
                    _MODULE,
                    "audit",
                    str(second.fileno()),
                    str(leaf),
                    *parent_namespaces,
                ],
                cwd="/",
                env=_SAFE_ENV,
                stdin=subprocess.DEVNULL,
                pass_fds=(second.fileno(),),
            )
            second.close()
            _send(channel, {"pid_namespace": os.readlink("/proc/self/ns/pid")}, (first.fileno(),))
            first.close()
            channel.settimeout(12)
            if probe:
                request, _ = _receive(channel, resources)
                if (
                    request != {"probe": "complete"}
                    or auditor.wait(timeout=5) != 0
                    or _cgroup_identity(leaf) != identity
                ):
                    raise CapabilityUnavailableError(
                        "Scoped worker probe did not shut down cleanly"
                    )
                with join.open("rb") as helper:
                    helper_digest = hashlib.file_digest(helper, "sha256").hexdigest()
                _send(channel, {"probe": "complete", "join_sha256": helper_digest})
                return
            with ExitStack() as descriptors:
                request, fds = _receive(channel, descriptors, _MAX_FDS + 3)
                _validate_worker(request, fds)
                if _cgroup_identity(leaf) != identity:
                    raise CapabilityUnavailableError("Scoped worker cgroup changed before exec")
                spec = os.memfd_create("potbot-worker-command", os.MFD_CLOEXEC)
                descriptors.callback(os.close, spec)
                with os.fdopen(os.dup(spec), "wb") as writer:
                    writer.write(json.dumps(request, ensure_ascii=True).encode("ascii"))
                os.lseek(spec, 0, os.SEEK_SET)
                worker = subprocess.Popen(
                    [
                        str(join),
                        str(leaf),
                        "--",
                        *_credentials(uid, gid),
                        sys.executable,
                        "-I",
                        "-m",
                        _MODULE,
                        "exec",
                        str(spec),
                        *map(str, fds[3:]),
                    ],
                    cwd="/",
                    env=_SAFE_ENV,
                    stdin=fds[0],
                    stdout=fds[1],
                    stderr=fds[2],
                    pass_fds=(spec, *fds[3:]),
                )
            for child in (worker, auditor):
                pidfd = os.pidfd_open(child.pid)
                resources.callback(os.close, pidfd)
                if child is worker:
                    worker_exit = pidfd
                else:
                    auditor_exit = pidfd
            ready, _, _ = select.select([channel, worker_exit, auditor_exit], [], [])
            if worker_exit not in ready:
                raise CapabilityUnavailableError("Scoped worker lost its launcher or collector")
            _send(channel, {"exit": worker.wait()})
            # The controller reconciles traces and closes the auditor after it reaps the proxy.
            if auditor.wait(timeout=30) != 0:
                raise CapabilityUnavailableError("Scoped worker collector exited unsuccessfully")
        finally:
            if worker is not None and worker.poll() is None:
                with os.fdopen(os.open("cgroup.kill", os.O_WRONLY, dir_fd=cgroup_fd), "wb") as kill:
                    kill.write(b"1\n")
                worker.kill()
                worker.wait(timeout=5)
            if auditor is not None and auditor.poll() is None:
                auditor.kill()
                auditor.wait(timeout=5)


def _accept(root: Path, join: Path, uid: int, gid: int, auditor_uid: int) -> int:
    if os.geteuid() != 0 or not 0 < uid != auditor_uid or auditor_uid <= 0 or gid <= 0:
        raise CapabilityUnavailableError("Invalid privileged launcher configuration")
    if (
        not root.is_relative_to("/sys/fs/cgroup")
        or root == Path("/sys/fs/cgroup")
        or root.resolve(strict=True) != root
        or root.stat().st_uid != uid
        or join.resolve(strict=True) != join
        or any(
            path.stat().st_uid != 0 or path.stat().st_mode & 0o022 for path in (join, *join.parents)
        )
    ):
        raise CapabilityUnavailableError("Scoped worker launcher provisioning is unavailable")
    # systemd delivers the accepted socket on stdin. Preserve a separate
    # descriptor across unshare, where stdin is explicitly replaced.
    with (
        socket.fromfd(0, socket.AF_UNIX, socket.SOCK_SEQPACKET) as channel,
        ExitStack() as resources,
    ):
        if _peer(channel)[1:] != (uid, gid):
            raise CapabilityUnavailableError("Scoped worker launch peer is not the configured user")
        channel.settimeout(5)
        request, _ = _receive(channel, resources)
        probe = set(request) == {"worker_id", "probe"} and request["probe"] is True
        if not probe and set(request) != {"worker_id"}:
            raise CapabilityUnavailableError("Invalid scoped worker allocation")
        worker_id = UUID(request["worker_id"])
        leaf = root / worker_id.hex
        if leaf.lstat().st_uid != uid:
            raise CapabilityUnavailableError("Worker cgroup ownership mismatch")
        return subprocess.run(
            [
                "/usr/bin/unshare",
                "--fork",
                "--kill-child",
                "--pid",
                "--net",
                "--mount-proc",
                sys.executable,
                "-I",
                "-m",
                _MODULE,
                "scope",
                str(channel.fileno()),
                str(leaf),
                str(uid),
                str(gid),
                str(auditor_uid),
                str(join),
                os.readlink("/proc/self/ns/pid"),
                os.readlink("/proc/self/ns/net"),
                *(["probe"] if probe else []),
            ],
            cwd="/",
            env=_SAFE_ENV,
            stdin=subprocess.DEVNULL,
            pass_fds=(channel.fileno(),),
        ).returncode


def main() -> int:
    try:
        match sys.argv[1:]:
            case ["proxy", fd, raw_fds, *argv]:
                return _proxy(int(fd), [int(x) for x in raw_fds.split(",") if x], argv)
            case ["exec", fd, *received]:
                _worker_exec(int(fd), [int(x) for x in received])
            case ["scope", fd, leaf, uid, gid, auditor, join, pid_ns, net_ns, *mode] if mode in (
                [],
                ["probe"],
            ):
                with socket.socket(fileno=int(fd)) as channel:
                    _scope(
                        channel,
                        Path(leaf),
                        int(uid),
                        int(gid),
                        int(auditor),
                        Path(join),
                        (pid_ns, net_ns),
                        probe=bool(mode),
                    )
                return 0
            case ["audit", fd, leaf, pid_ns, net_ns]:
                serve_scoped_taskstats(
                    socket.socket(fileno=int(fd)),
                    UUID(hex=Path(leaf).name),
                    Path(leaf),
                    None,
                    parent_namespaces=(pid_ns, net_ns),
                )
                return 0
            case ["accept", root, join, uid, gid, auditor]:
                return _accept(Path(root), Path(join), int(uid), int(gid), int(auditor))
            case _:
                raise CapabilityUnavailableError("Invalid scoped launcher operation")
    except CapabilityUnavailableError as error:
        if len(sys.argv) > 1 and sys.argv[1] in {"accept", "scope", "audit"}:
            print(f"Scoped worker {sys.argv[1]}: {error}", file=sys.stderr)
        return 125
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if len(sys.argv) > 1 and sys.argv[1] in {"accept", "scope", "audit"}:
            print(
                f"Scoped worker {sys.argv[1]}: {type(error).__name__}",
                file=sys.stderr,
            )
        return 125
    return 125


if __name__ == "__main__":
    raise SystemExit(main())
