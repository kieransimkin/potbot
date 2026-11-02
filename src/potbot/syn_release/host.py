"""Explicit native host provisioning. Windows owns one managed WSL2 distro.

No credential/identity is fabricated, imported or unlocked by Potbot. Native
keystore initialization/unlock is a separate private Syn terminal operation.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

from potbot.domain.errors import CapabilityUnavailableError, StateMismatchError
from potbot.tools.temporary import create_directory

DISTRO = "Potbot-SynOperator"
UBUNTU = "Ubuntu-24.04"
OPERATOR_USER = "synoperator"


def is_windows() -> bool:
    return os.name == "nt"


# All executable shell text is fixed reviewed source; no model/user field is
# interpolated into root commands. Only this newly provisioned distro is used.
PROVISION_UBUNTU = r"""set -eu
test "$(id -u)" = 0
. /etc/os-release
test "$ID" = ubuntu
test "$VERSION_ID" = 24.04
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install --yes --no-install-recommends ca-certificates python3.12 python3.12-venv python3.12-dev \
    gnome-keyring dbus-user-session libsecret-tools build-essential libgmp-dev
if ! id synoperator >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash synoperator
fi
test "$(id -u synoperator)" -ne 0
install -d -m 0700 -o synoperator -g synoperator /home/synoperator/.local
install -d -m 0700 -o synoperator -g synoperator /home/synoperator/.local/share
install -d -m 0700 -o synoperator -g synoperator /home/synoperator/.local/share/potbot
command -v gnome-keyring-daemon >/dev/null
command -v dbus-run-session >/dev/null
loginctl enable-linger synoperator
printf 'syn-native-prerequisites-ready\n'
"""


def _decode(raw: bytes) -> str:
    return raw.decode("utf-16-le" if b"\0" in raw else "utf-8", errors="replace").lstrip("\ufeff")


def run_host(argv: list[str], *, timeout: int = 30) -> str:
    # Output goes to a bounded caller-controlled process, never a hidden shell.
    # Provisioning has a fixed wall-clock cap and cannot unregister a distro.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    result = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        check=False,
        creationflags=flags,
    )
    if len(result.stdout) + len(result.stderr) > 4 * 1024 * 1024:
        raise StateMismatchError("Native setup diagnostics exceeded their bound")
    if result.returncode != 0:
        raise CapabilityUnavailableError(
            "Native host setup needs local administrator/restart or package-manager attention"
        )
    return _decode(result.stdout)


class WslOperatorHost:
    def __init__(self, root: Path | None = None):
        if not is_windows():
            raise CapabilityUnavailableError("Managed WSL setup is a Windows host operation")
        self.root = root or Path(os.environ["LOCALAPPDATA"]) / "Potbot" / "SynOperator"
        if (
            not self.root.is_absolute()
            or self.root.is_symlink()
            or self.root.resolve() != self.root
        ):
            raise StateMismatchError("Managed WSL storage requires a fixed local operator path")
        self.wsl = str(Path(os.environ["SystemRoot"]) / "System32/wsl.exe")
        self.receipt = self.root / "host.json"

    def _installed(self) -> set[str]:
        return {
            line.strip()
            for line in run_host([self.wsl, "--list", "--quiet"]).splitlines()
            if line.strip()
        }

    def setup(self) -> dict[str, Any]:
        create_directory(self.root)
        installed = self._installed()
        if self.receipt.exists():
            record: dict[str, Any] = json.loads(self.receipt.read_text())
            if record.get("profile") != "potbot.syn-wsl-host/1" or record.get("distro") != DISTRO:
                raise StateMismatchError("Managed WSL ownership receipt is invalid")
            if (
                record.get("state") not in {"installing", "ready"}
                or record.get("user") != OPERATOR_USER
            ):
                raise StateMismatchError("Managed WSL lifecycle/owner is invalid")
            if DISTRO not in installed:
                raise StateMismatchError(
                    "Registered operator distro is missing; no silent replacement"
                )
            if record["state"] == "ready" and record.get("prerequisite_revision") == 3:
                self.start_keystore()
                return record
            # Explicit repeated host-setup may resume package prerequisites.
            # It never deletes an image, replaces an identity or adopts a name
            # which predates this host's retained installing receipt.
        else:
            if DISTRO in installed:
                raise StateMismatchError("Existing named WSL distro has no ownership receipt")
            record = {
                "profile": "potbot.syn-wsl-host/1",
                "installation_id": uuid4().hex,
                "distro": DISTRO,
                "distribution": UBUNTU,
                "user": OPERATOR_USER,
                "state": "installing",
                "location": str(self.root / "distro"),
            }
            self.receipt.write_text(json.dumps(record, sort_keys=True) + "\n")
            run_host(
                [
                    self.wsl,
                    "--install",
                    "--distribution",
                    UBUNTU,
                    "--name",
                    DISTRO,
                    "--location",
                    str(self.root / "distro"),
                    "--no-launch",
                    "--web-download",
                ],
                timeout=1800,
            )
        run_host(
            [
                self.wsl,
                "--distribution",
                DISTRO,
                "--user",
                "root",
                "--exec",
                "/bin/sh",
                "-c",
                PROVISION_UBUNTU,
            ],
            timeout=1200,
        )
        self.start_keystore()
        record = {
            **record,
            "state": "ready",
            "prerequisite_revision": 3,
            "custody": "linux-secret-service",
            "keystore_unlocked": False,
            "operator_identity_created": False,
        }
        temporary = self.root / (uuid4().hex + ".json")
        temporary.write_text(json.dumps(record, sort_keys=True) + "\n")
        temporary.replace(self.receipt)
        return record

    def start_keystore(self) -> None:
        run_host(
            [
                self.wsl,
                "--distribution",
                DISTRO,
                "--user",
                OPERATOR_USER,
                "--exec",
                "/usr/bin/systemctl",
                "--user",
                "start",
                "gnome-keyring-daemon.service",
            ]
        )

    def command(self, executable: str, arguments: list[str]) -> list[str]:
        if not executable.startswith("/") or "\0" in executable:
            raise StateMismatchError("WSL dispatch requires an absolute native executable")
        record: dict[str, Any] = json.loads(self.receipt.read_text())
        if (
            record.get("state") != "ready"
            or record.get("distro") != DISTRO
            or DISTRO not in self._installed()
        ):
            raise StateMismatchError("Managed WSL host is not ready")
        return [
            self.wsl,
            "--distribution",
            DISTRO,
            "--user",
            OPERATOR_USER,
            "--exec",
            executable,
            *arguments,
        ]


def native_host_setup() -> dict[str, Any]:
    if platform.system() == "Windows":
        return WslOperatorHost().setup()
    if platform.system() == "Darwin":
        return {
            "profile": "potbot.syn-native-host/1",
            "host": "macos",
            "custody": "macos-keychain",
            "keystore_unlocked": False,
            "operator_identity_created": False,
        }
    if platform.system() == "Linux":
        # Native desktop Linux reuses its own user/session. Administrative
        # package installation is explicit; this fixed route targets apt hosts.
        argv = [
            "/usr/bin/apt-get",
            "install",
            "--yes",
            "gnome-keyring",
            "dbus-user-session",
            "libsecret-tools",
            "build-essential",
            "libgmp-dev",
            "python3-dev",
        ]
        if os.getuid() != 0:
            argv = ["/usr/bin/sudo", *argv]
        result = subprocess.run(argv, check=False)
        if result.returncode:
            raise CapabilityUnavailableError("Native credential-service package setup failed")
        return {
            "profile": "potbot.syn-native-host/1",
            "host": "linux",
            "custody": "linux-secret-service",
            "keystore_unlocked": False,
            "operator_identity_created": False,
        }
    raise CapabilityUnavailableError("No qualified operator host profile")
