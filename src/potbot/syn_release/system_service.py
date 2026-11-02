"""Install one explicit user service around Potbot's verified operator supervisor."""

from __future__ import annotations

import os
import platform
import plistlib
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.syn_release.operator import SynOperatorDeployment
from potbot.syn_release.store import _write_new, private_read


def user_service_directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if (
        path.resolve() != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
    ):
        raise ScopeDeniedError(
            "Native user-service directory has an unsafe owner or writable parent"
        )


def read_user_unit(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or not 0 < info.st_size <= 65536
        ):
            raise ScopeDeniedError("Native user-service unit is not owned and private")
        raw = os.read(fd, 65537)
        if len(raw) != info.st_size:
            raise StateMismatchError("Native user-service unit changed during read")
        return raw
    finally:
        os.close(fd)


def systemd_argument(value: str) -> str:
    if any(ord(ch) < 32 for ch in value):
        raise ScopeDeniedError("Native service argument contains control characters")
    return (
        '"'
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        + '"'
    )


def supervisor_arguments(deployment: SynOperatorDeployment, policy_path: Path) -> list[str]:
    if not policy_path.is_absolute():
        raise ScopeDeniedError("Native service policy requires an absolute private path")
    private_read(policy_path)
    return [
        sys.executable,
        "-I",
        "-B",
        "-m",
        "potbot",
        "syn-operator",
        "serve",
        "--store",
        str(deployment.store.root),
        "--policy",
        str(policy_path),
        "--operator-root",
        str(deployment.root),
    ]


def install_service(deployment: SynOperatorDeployment, policy_path: Path) -> dict[str, Any]:
    import pwd

    # A missing/corrupt operator registry is never repaired by installing a unit.
    deployment._registered()
    deployment._installation()
    argv = supervisor_arguments(deployment, policy_path)
    home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if platform.system() == "Linux":
        parent = home
        for name in (".config", "systemd", "user"):
            parent /= name
            user_service_directory(parent)
        path = parent / "potbot-syn-operator.service"
        runtime = f"/run/user/{os.getuid()}"
        raw = (
            "[Unit]\nDescription=Verified Syn operator supervised by Potbot\n"
            "After=gnome-keyring-daemon.service\nRequires=gnome-keyring-daemon.service\n\n"
            "[Service]\nType=simple\nExecStart="
            + " ".join(systemd_argument(x) for x in argv)
            + "\nEnvironment=XDG_RUNTIME_DIR="
            + runtime
            + "\nEnvironment=DBUS_SESSION_BUS_ADDRESS=unix:path="
            + runtime
            + "/bus"
            + "\nRestart=no\nKillMode=control-group\nTimeoutStopSec=15\nUMask=0077\n\n"
            + "[Install]\nWantedBy=default.target\n"
        ).encode()
        commands = [
            ["/usr/bin/systemctl", "--user", "daemon-reload"],
            ["/usr/bin/systemctl", "--user", "enable", "--now", path.name],
        ]
    elif platform.system() == "Darwin":
        parent = home / "Library" / "LaunchAgents"
        user_service_directory(home / "Library")
        user_service_directory(parent)
        path = parent / "co.synsphere.potbot-operator.plist"
        raw = plistlib.dumps(
            {
                "Label": "co.synsphere.potbot-operator",
                "ProgramArguments": argv,
                "RunAtLoad": True,
                "KeepAlive": False,
                "StandardOutPath": str(deployment.root / "service-output.log"),
                "StandardErrorPath": str(deployment.root / "service-error.log"),
            }
        )
        target = f"gui/{os.getuid()}/co.synsphere.potbot-operator"
        existing = subprocess.run(
            ["/bin/launchctl", "print", target], capture_output=True, timeout=10, check=False
        )
        commands = (
            []
            if existing.returncode == 0
            else [["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)]]
        )
        commands.append(["/bin/launchctl", "kickstart", target])
    else:
        raise ScopeDeniedError(
            "Install the operator user service inside WSL or the native POSIX host"
        )
    if path.exists():
        if read_user_unit(path) != raw:
            raise StateMismatchError(
                "Existing user service differs; explicit lifecycle reconciliation is required"
            )
    else:
        _write_new(path, raw)
    for command in commands:
        result = subprocess.run(command, capture_output=True, timeout=30, check=False)
        if result.returncode:
            raise StateMismatchError(
                "Native user service did not start; retained identity is not replaced"
            )
    return {
        "profile": "potbot.syn-operator-user-service/1",
        "unit": str(path),
        "state": "start_requested",
        "identity_recreated": False,
    }
