"""Provision the dedicated systemd launcher and accounting subtree on Linux."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from potbot.domain.errors import CapabilityUnavailableError
from potbot.tools.scoped_launcher import DEFAULT_SOCKET

SOCKET = "potbot-scoped-worker.socket"
BROKER = "potbot-scoped-worker@.service"
KEEPER = "potbot-scoped-worker-cgroups.service"
CGROUP = Path("/sys/fs/cgroup/system.slice") / KEEPER
CONTROLLERS = ("cpu", "io", "memory", "pids")
UNIT_DIR = Path("/etc/systemd/system")
ENV_FILE = Path("/etc/potbot-scoped-worker/monitor.env")
AUDIT = Path("/var/lib/potbot-scoped-worker/audit")
LOCK = Path("/run/potbot-scoped-worker-provision.lock")
SYSTEMCTL = "/usr/bin/systemctl"
MODULE = "potbot.tools.scoped_provision"


def _protected(path: Path) -> None:
    if not path.is_absolute() or not re.fullmatch(r"/[A-Za-z0-9_/+.@\-]+", str(path)):
        raise CapabilityUnavailableError("Use a protected absolute path without spaces.")
    resolved = path.resolve(strict=True)
    for part in {path, *path.parents, resolved, *resolved.parents}:
        if part.is_symlink():
            # A root-owned Python venv may link to the trusted system interpreter.
            if part.lstat().st_uid != 0:
                raise CapabilityUnavailableError("A launcher path has an untrusted link.")
        else:
            info = part.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise CapabilityUnavailableError("A launcher path is writable by another user.")


def _executable(path: Path, digest: str | None = None) -> None:
    _protected(path)
    info = path.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or not info.st_mode & 0o111
        or info.st_mode & (stat.S_ISUID | stat.S_ISGID)
        or (digest is not None and path.resolve() != path)
    ):
        raise CapabilityUnavailableError("Expected a protected executable without set-ID bits.")
    if digest is not None:
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise CapabilityUnavailableError("A monitoring executable differs from its pin.")


def _units(
    python: Path,
    join: Path,
    tracer: Path,
    join_sha: str,
    tracer_sha: str,
    uid: int,
    gid: int,
    auditor: int,
) -> dict[Path, str]:
    return {
        UNIT_DIR / KEEPER: (
            "[Unit]\nDescription=Potbot scoped worker accounting subtree\n"
            "[Service]\nType=notify\nNotifyAccess=main\nSlice=system.slice\n"
            f"User={uid}\nGroup={gid}\n"
            f"Delegate={' '.join(CONTROLLERS)}\nDelegateSubgroup=keeper\n"
            f"ExecStart={python} -I -m {MODULE} hold\n"
            "NoNewPrivileges=yes\nTimeoutStartSec=15\n"
        ),
        UNIT_DIR / BROKER: (
            "[Unit]\nDescription=Potbot scoped worker launcher instance\n"
            "[Service]\nType=simple\nStandardInput=socket\nStandardOutput=journal\n"
            f"ExecStart={python} -I -m potbot.tools.scoped_launcher "
            f"accept {CGROUP} {join} {uid} {gid} {auditor}\n"
            "KillMode=control-group\nTimeoutStopSec=10\n"
        ),
        UNIT_DIR / SOCKET: (
            "[Unit]\nDescription=Potbot scoped worker launcher\n"
            f"BindsTo={KEEPER}\nAfter={KEEPER}\n"
            "[Socket]\nAccept=yes\nRemoveOnStop=yes\n"
            f"ListenSequentialPacket={DEFAULT_SOCKET}\n"
            f"SocketUser=root\nSocketGroup={gid}\nSocketMode=0660\n"
            "[Install]\nWantedBy=sockets.target\n"
        ),
        ENV_FILE: (
            f"POTBOT_OPERATOR_AUDIT_ROOT={AUDIT}\n"
            f"POTBOT_OPERATOR_CGROUP_ROOT={CGROUP}\n"
            f"POTBOT_OPERATOR_STRACE={tracer}\nPOTBOT_OPERATOR_STRACE_SHA256={tracer_sha}\n"
            f"POTBOT_OPERATOR_CGROUP_JOIN={join}\nPOTBOT_OPERATOR_CGROUP_JOIN_SHA256={join_sha}\n"
        ),
    }


def _manager(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [SYSTEMCTL, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=35,
        env={"PATH": "/usr/sbin:/usr/bin", "LANG": "C"},
    )
    if check and result.returncode:
        raise CapabilityUnavailableError(
            "systemd refused launcher provisioning; inspect its journal."
        )
    return result


def _inspect(path: Path, content: str) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    _protected(path)
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o644
        or info.st_size != len(content)
        or path.read_bytes() != content.encode("ascii")
    ):
        raise CapabilityUnavailableError(
            "A launcher file differs; existing configuration was kept."
        )
    return True


def _write(path: Path, text: str) -> None:
    # Publish without replacing a file, even if it appeared since preflight.
    import tempfile

    fd, temporary = tempfile.mkstemp(prefix=".potbot-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(text.encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
    finally:
        os.unlink(temporary)


def _audit_directory(uid: int, gid: int) -> bool:
    try:
        AUDIT.mkdir(mode=0o700)
    except FileExistsError:
        info = AUDIT.lstat()
        if not stat.S_ISDIR(info.st_mode) or (
            info.st_uid,
            info.st_gid,
            stat.S_IMODE(info.st_mode),
        ) != (uid, gid, 0o700):
            raise CapabilityUnavailableError(
                "Existing audit directory has a different owner or mode."
            ) from None
        return False
    try:
        os.chmod(AUDIT, 0o700)
        os.chown(AUDIT, uid, gid)
    except OSError:
        AUDIT.rmdir()
        raise
    return True


def _apply(files: dict[Path, str], uid: int, gid: int) -> None:
    pending = {path: data for path, data in files.items() if not _inspect(path, data)}
    for name in (KEEPER, BROKER, SOCKET):
        instance = name.replace("@.", "@provision-check.")
        props = _manager("show", "-p", "FragmentPath", "-p", "DropInPaths", instance).stdout
        values = dict(line.split("=", 1) for line in props.splitlines() if "=" in line)
        if values.get("FragmentPath", "") not in ("", str(UNIT_DIR / name)) or values.get(
            "DropInPaths"
        ):
            raise CapabilityUnavailableError("Existing systemd launcher configuration was kept.")

    active = _manager("is-active", "--quiet", SOCKET, check=False).returncode == 0
    keeper_active = _manager("is-active", "--quiet", KEEPER, check=False).returncode == 0
    enable_state = _manager("is-enabled", SOCKET, check=False).stdout.strip()
    enabled = enable_state == "enabled"
    wants = UNIT_DIR / "sockets.target.wants" / SOCKET
    if enable_state not in ("", "not-found", "disabled", "enabled") or (
        (wants.exists() or wants.is_symlink())
        and (not wants.is_symlink() or wants.resolve() != UNIT_DIR / SOCKET)
    ):
        raise CapabilityUnavailableError(
            "An existing socket enablement needs administrator review."
        )
    if (pending and (active or keeper_active or enabled)) or (
        (DEFAULT_SOCKET.exists() or DEFAULT_SOCKET.is_symlink()) and not active
    ):
        raise CapabilityUnavailableError(
            "An existing launcher is in use or needs administrator review."
        )

    created: list[Path] = []
    directories: list[Path] = []
    made_audit = False
    enabling = starting = False
    try:
        for directory in (ENV_FILE.parent, AUDIT.parent):
            if not directory.exists() and not directory.is_symlink():
                directory.mkdir(mode=0o755)
                directories.append(directory)
            _protected(directory)
            if directory.is_symlink():
                raise CapabilityUnavailableError(
                    "A launcher directory must not be a symbolic link."
                )
        made_audit = _audit_directory(uid, gid)
        for path, data in pending.items():
            _write(path, data)
            created.append(path)
        if created:
            _manager("daemon-reload")
        if not enabled:
            enabling = True
            _manager("enable", SOCKET)
        if not active:
            starting = True
            _manager("start", SOCKET)
        info = DEFAULT_SOCKET.lstat()
        if (
            not stat.S_ISSOCK(info.st_mode)
            or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (0, gid, 0o660)
            or CGROUP.stat().st_uid != uid
            or not set(CONTROLLERS) <= set((CGROUP / "cgroup.subtree_control").read_text().split())
        ):
            raise CapabilityUnavailableError(
                "Installed launcher or delegated controllers failed readback."
            )
    except (CapabilityUnavailableError, OSError, subprocess.TimeoutExpired):
        # Never stop an already active launcher or remove a pre-existing file.
        try:
            if starting:
                _manager("stop", SOCKET, *([] if keeper_active else [KEEPER]))
            if enabling:
                _manager("disable", SOCKET)
            for path in reversed(created):
                path.unlink()
            if created:
                _manager("daemon-reload")
            if made_audit:
                AUDIT.rmdir()
            for path in reversed(directories):
                path.rmdir()
        except (CapabilityUnavailableError, OSError, subprocess.TimeoutExpired):
            raise CapabilityUnavailableError(
                "Provisioning rollback needs administrator review."
            ) from None
        raise


def provision(args: argparse.Namespace) -> None:
    import fcntl
    import pwd

    if os.geteuid() != 0 or not sys.flags.isolated:
        raise CapabilityUnavailableError(
            "Run the administrator-owned Python installation with -I as root."
        )
    _protected(UNIT_DIR)
    _protected(LOCK.parent)
    python = Path(sys.executable)
    _executable(python)
    _protected(Path(__file__))
    _executable(args.join, args.join_sha256)
    _executable(args.strace, args.strace_sha256)
    service, auditor = pwd.getpwnam(args.service_user), pwd.getpwnam(args.auditor_user)
    uid, gid = service.pw_uid, service.pw_gid
    if (
        min(uid, gid, auditor.pw_uid) <= 0
        or auditor.pw_uid == uid
        or auditor.pw_uid != auditor.pw_gid
        or auditor.pw_gid == gid
    ):
        raise CapabilityUnavailableError("Use separate unprivileged service and auditor accounts.")
    fd = os.open(LOCK, os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_RDWR, 0o600)
    with os.fdopen(fd, "rb+") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise CapabilityUnavailableError("Provisioning lock is untrusted.")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _apply(
            _units(
                python,
                args.join,
                args.strace,
                args.join_sha256,
                args.strace_sha256,
                uid,
                gid,
                auditor.pw_uid,
            ),
            uid,
            gid,
        )


def _hold() -> None:
    expected = "0::" + str(CGROUP / "keeper").removeprefix("/sys/fs/cgroup")
    if (
        os.geteuid() == 0
        or Path("/proc/self/cgroup").read_text().splitlines() != [expected]
        or not set(CONTROLLERS) <= set((CGROUP / "cgroup.controllers").read_text().split())
        or (CGROUP / "cgroup.procs").read_text().strip()
    ):
        raise CapabilityUnavailableError("systemd did not delegate the required worker cgroup.")
    (CGROUP / "cgroup.subtree_control").write_text(" ".join("+" + name for name in CONTROLLERS))
    notify = os.environ["NOTIFY_SOCKET"]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
        channel.sendto(b"READY=1", "\x00" + notify[1:] if notify.startswith("@") else notify)
    while True:
        signal.pause()


def _digest(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise argparse.ArgumentTypeError("expected lowercase SHA-256")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    install = commands.add_parser("install", help="install and enable the systemd launcher")
    for name in ("service-user", "auditor-user"):
        install.add_argument("--" + name, required=True)
    for name in ("join", "strace"):
        install.add_argument("--" + name, type=Path, required=True)
        install.add_argument("--" + name + "-sha256", type=_digest, required=True)
    commands.add_parser("hold", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if not sys.platform.startswith("linux"):
            raise CapabilityUnavailableError("Scoped launcher provisioning requires Linux systemd.")
        if args.action == "hold":
            _hold()
        else:
            provision(args)
            print(json.dumps({"launcher_installed": True, "worker_qualified": False}))
        return 0
    except CapabilityUnavailableError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (KeyError, OSError, subprocess.TimeoutExpired):
        print(
            "Scoped launcher provisioning failed; inspect local systemd diagnostics.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
