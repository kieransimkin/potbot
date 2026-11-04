"""Linux namespace launch for a verified Syn Agent package and one native UID.

Reuses Potbot's maintained libseccomp primitive. A distinct host UID is retained
through the user namespace; mapping every Agent back to root is prohibited.
"""

from __future__ import annotations

import os
import stat
import subprocess
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from potbot.domain.errors import CapabilityUnavailableError, ScopeDeniedError
from potbot.tools.isolation import IsolationLaunch, _seccomp_fd
from potbot.tools.temporary import TemporaryDirectory

from .agent_host import agent_log_directory


@contextmanager
def native_user_namespace(uid: int, gid: int) -> Iterator[int]:
    if os.name != "posix" or os.getuid() != 0 or not 20000 <= uid <= 59999 or gid != uid:
        raise ScopeDeniedError("Agent namespace requires its registered restricted native UID")
    keeper = subprocess.Popen(
        [
            "/usr/bin/unshare",
            "--user",
            "--map-root-user",
            f"--map-users={uid}:{uid}:1",
            f"--map-groups={gid}:{gid}:1",
            "/usr/bin/sleep",
            "30",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    fd = -1
    try:
        deadline = time.monotonic() + 5
        expected = {(0, 0, 1), (uid, uid, 1)}
        while True:
            if keeper.poll() is not None:
                raise CapabilityUnavailableError("Native Agent namespace mapping failed")
            try:
                user_map = {
                    tuple(map(int, row.split()))
                    for row in Path(f"/proc/{keeper.pid}/uid_map").read_text().splitlines()
                }
                group_map = {
                    tuple(map(int, row.split()))
                    for row in Path(f"/proc/{keeper.pid}/gid_map").read_text().splitlines()
                }
            except (FileNotFoundError, ProcessLookupError):
                user_map = group_map = set()
            if user_map == group_map == expected:
                fd = os.open(f"/proc/{keeper.pid}/ns/user", os.O_RDONLY | os.O_CLOEXEC)
                break
            if time.monotonic() >= deadline:
                raise CapabilityUnavailableError(
                    "Native Agent namespace mapping did not become ready"
                )
            time.sleep(0.02)
        # The owned namespace FD retains the namespace; no keeper is left running.
        keeper.terminate()
        keeper.wait(timeout=5)
        yield fd
    finally:
        if keeper.poll() is None:
            keeper.kill()
        keeper.wait(timeout=5)
        if fd >= 0:
            os.close(fd)


def _read_only_root(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise ScopeDeniedError("Syn runtime mounts must be canonical absolute directories")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ScopeDeniedError("Syn sandbox runtime must be root-owned and not writable by Agents")
    if path in {
        Path("/"),
        Path("/home"),
        Path("/var"),
        Path("/var/lib"),
        Path("/mnt"),
        Path("/run"),
    }:
        raise ScopeDeniedError("A broad private host tree cannot be a Syn runtime mount")
    return path


@contextmanager
def prepared_launch(
    *,
    host: Mapping[str, Any],
    python: Path,
    runtime_roots: tuple[Path, ...],
    arguments: tuple[str, ...],
) -> Iterator[IsolationLaunch]:
    """Only the trusted deployment controller chooses the verified package roots.

    Only the Agent's own home and log directory persist; /tmp and /run are private tmpfs.
    The only network capability is AF_UNIX within this isolated mount namespace.
    """
    if os.name != "posix" or os.getuid() != 0:
        raise ScopeDeniedError("Syn Agent sandbox construction requires native root")
    uid, gid, home = host["uid"], host["gid"], Path(host["home"])
    info = home.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or home.is_symlink()
        or info.st_uid != uid
        or info.st_gid != gid
        or info.st_mode & 0o077
    ):
        raise ScopeDeniedError("Syn Agent home differs from its native account registration")
    logs = agent_log_directory(home, uid, gid)
    roots = tuple(_read_only_root(path) for path in runtime_roots)
    if any(path.is_relative_to(home.parent) or home.parent.is_relative_to(path) for path in roots):
        raise ScopeDeniedError("Syn runtime mounts must be separate from private Agent state")
    log_mount = Path("/var/logs/ai")
    if any(path.is_relative_to(log_mount) or log_mount.is_relative_to(path) for path in roots):
        raise ScopeDeniedError("Syn runtime mounts overlap the reserved Agent log path")
    if not any(python.is_relative_to(path) for path in roots) or python.is_relative_to(home):
        raise ScopeDeniedError("Syn Agent interpreter is outside the verified read-only runtime")
    executable = Path("/usr/bin/bwrap")
    tool = executable.stat()
    if tool.st_uid != 0 or tool.st_mode & 0o022 or not stat.S_ISREG(tool.st_mode):
        raise ScopeDeniedError("Bubblewrap must be a root-owned native executable")
    with (
        TemporaryDirectory(prefix="syn-agent-run-", dir=home.parent) as transient,
        native_user_namespace(uid, gid) as userns,
    ):
        runtime = Path(transient) / "runtime"
        runtime.mkdir(mode=0o700)
        os.chown(runtime, uid, gid)
        seccomp = _seccomp_fd(private_syn_unix=True)
        try:
            argv = [
                str(executable),
                "--userns",
                str(userns),
                "--unshare-pid",
                "--unshare-net",
                "--unshare-ipc",
                "--unshare-uts",
                "--unshare-cgroup",
                "--die-with-parent",
                "--uid",
                "0",
                "--gid",
                "0",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "CAP_SETUID",
                "--cap-add",
                "CAP_SETGID",
                "--cap-add",
                "CAP_SETPCAP",
                "--clearenv",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--chmod",
                "1777",
                "/tmp",
                "--tmpfs",
                "/run",
                "--bind",
                str(runtime),
                f"/run/user/{uid}",
            ]
            # Auto-created Bubblewrap ancestors may be 0700. These are empty
            # namespace-local placeholders, never broad binds of host trees.
            ancestors = {Path(p) for p in ("/etc", "/etc/dbus-1", "/run/user", "/var", "/var/logs")}
            for destination in (home, *roots):
                ancestors.update(parent for parent in destination.parents if parent != Path("/"))
            for ancestor in sorted(ancestors, key=lambda item: (len(item.parts), str(item))):
                argv.extend(("--dir", str(ancestor), "--chmod", "0755", str(ancestor)))
            # A protected runtime prefix may itself live below /tmp. Empty
            # ancestor placeholders must not remove the private tmpfs's sticky
            # writable mode, which native D-Bus needs for its own sockets.
            argv.extend(("--chmod", "1777", "/tmp"))
            for path in (Path("/usr"), Path("/lib"), Path("/lib64")):
                if path.exists():
                    argv.extend(("--ro-bind", str(path), str(path)))
            for text_path in ("/etc/passwd", "/etc/group", "/etc/nsswitch.conf", "/etc/machine-id"):
                argv.extend(("--ro-bind", text_path, text_path))
            for path in roots:
                if path != Path("/usr"):
                    argv.extend(("--ro-bind", str(path), str(path)))
            argv.extend(
                (
                    "--bind",
                    str(home),
                    str(home),
                    "--bind",
                    str(logs),
                    str(log_mount),
                    "--setenv",
                    "POTBOT_AGENT_LOG_DIR",
                    str(log_mount),
                    "--chdir",
                    "/",
                    "--setenv",
                    "HOME",
                    str(home),
                    "--setenv",
                    "PATH",
                    "/usr/bin:/bin",
                    "--setenv",
                    "LANG",
                    "C.UTF-8",
                    "--setenv",
                    "XDG_RUNTIME_DIR",
                    f"/run/user/{uid}",
                    "--setenv",
                    "XDG_DATA_HOME",
                    str(home / ".local/share"),
                    "--remount-ro",
                    "/",
                    "--seccomp",
                    str(seccomp),
                    "--",
                    "/usr/bin/setpriv",
                    f"--reuid={uid}",
                    f"--regid={gid}",
                    "--clear-groups",
                    "--no-new-privs",
                    "--bounding-set=-all",
                    "--inh-caps=-all",
                    "--ambient-caps=-all",
                    "/usr/bin/prlimit",
                    "--as=1073741824",
                    "--nofile=128",
                    "--nproc=64",
                    "--fsize=16777216",
                    "--",
                    "/usr/bin/dbus-run-session",
                    "--",
                    str(python),
                    "-I",
                    "-B",
                    "-m",
                    "syn.agent.session",
                    *arguments,
                )
            )
            yield IsolationLaunch(tuple(argv), (userns, seccomp), "syn-agent-native-uid-af-unix-v2")
        finally:
            os.close(seccomp)
