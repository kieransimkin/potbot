"""Root-owned Linux Agent account provisioning; no Syn identity or key code.

The registry records OS resource ownership, not Potbot task or Cog authority.
Every existing resource needs an exact receipt; ambiguity never causes repair.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.domain.errors import CapabilityUnavailableError, ScopeDeniedError, StateMismatchError

from .contracts import canonical, document
from .store import _directory, _write_new, private_read


def _root_file(path: Path) -> bytes:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
            or info.st_nlink != 1 or info.st_size > 65536):
        raise ScopeDeniedError("Native account policy has unsafe ownership, links or size")
    return path.read_bytes()


def _delegate(path: Path, uid: int) -> None:
    original = _root_file(path)
    line = f"root:{uid}:1".encode("ascii")
    if line in original.splitlines():
        return
    # Never overlap another principal's subordinate ranges. Root's own UID 0
    # maps separately; only this one assigned Agent account is delegated here.
    for row in original.splitlines():
        parts = row.split(b":")
        if len(parts) != 3:
            raise StateMismatchError("Malformed native subordinate-ID policy")
        start, count = int(parts[1]), int(parts[2])
        if start <= uid < start + count:
            raise StateMismatchError("Agent ID overlaps an existing subordinate-ID range")
    if path.read_bytes() != original:
        raise StateMismatchError("Native account policy changed during provisioning")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        current = os.fstat(fd)
        if current.st_uid != 0 or current.st_mode & 0o022 or current.st_nlink != 1:
            raise ScopeDeniedError("Unsafe subordinate-ID policy during append")
        os.write(fd, (b"" if not original or original.endswith(b"\n") else b"\n") + line + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    if line not in _root_file(path).splitlines():
        raise StateMismatchError("Native subordinate-ID delegation readback failed")


def provision(root: Path, *, agent_id: UUID, uid: int) -> dict[str, Any]:
    if os.name != "posix" or os.getuid() != 0 or not Path("/proc/self/ns/user").exists():
        raise CapabilityUnavailableError("Agent OS-account provisioning requires native Linux root")
    if type(agent_id) is not UUID or type(uid) is not int or not 20000 <= uid <= 59999:
        raise ScopeDeniedError("Agent provisioning requires an exact Agent UUID and restricted UID")
    import fcntl
    import time
    _directory(root, create=True)
    fd = os.open("/run/lock/potbot-syn-agent-provision.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ScopeDeniedError("Unsafe native Agent provisioning lock")
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StateMismatchError("Another controller owns native Agent provisioning") from None
                time.sleep(0.05)
        return _provision_locked(root, agent_id=agent_id, uid=uid)
    finally:
        os.close(fd)


def _provision_locked(root: Path, *, agent_id: UUID, uid: int) -> dict[str, Any]:
    import pwd
    name = "synag-" + agent_id.hex[:16]
    directory = root / agent_id.hex
    registration = directory / "host.json"
    home = directory / "home"
    expected = {"profile": "potbot.syn-agent-host/1", "agent_id": str(agent_id), "uid": uid,
                "gid": uid, "account": name, "home": str(home), "device_profile": "syn.host-bound-virtual-device/1"}
    if directory.exists() or directory.is_symlink():
        actual = document(private_read(registration), canonical_only=True)
        if actual != expected:
            raise StateMismatchError("Agent OS registration differs; no replacement")
        account = pwd.getpwnam(name)
        if (account.pw_uid != uid or account.pw_gid != uid or account.pw_dir != str(home)
                or account.pw_shell != "/usr/sbin/nologin" or home.is_symlink()
                or home.stat().st_uid != uid or home.stat().st_mode & 0o077):
            raise StateMismatchError("Registered Agent OS resources changed")
        for path in (Path("/etc/subuid"), Path("/etc/subgid")):
            if f"root:{uid}:1".encode() not in _root_file(path).splitlines():
                raise StateMismatchError("Registered Agent namespace delegation is absent")
        return actual
    try:
        pwd.getpwnam(name)
    except KeyError:
        pass
    else:
        raise StateMismatchError("Unregistered native Agent account already exists")
    try:
        pwd.getpwuid(uid)
    except KeyError:
        pass
    else:
        raise StateMismatchError("Selected Agent UID is already allocated")
    directory.mkdir(mode=0o700)
    _write_new(directory / "initializing.json", canonical(expected))
    result = subprocess.run(["/usr/sbin/useradd", "--no-create-home", "--no-log-init", "--user-group",
        "--uid", str(uid), "--home-dir", str(home), "--shell", "/usr/sbin/nologin", name],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
    if result.returncode:
        raise StateMismatchError("Native Agent account provisioning failed; reconcile retained marker")
    home.mkdir(mode=0o700)
    os.chown(home, uid, uid)
    _delegate(Path("/etc/subuid"), uid)
    _delegate(Path("/etc/subgid"), uid)
    _write_new(registration, canonical(expected))
    return expected
