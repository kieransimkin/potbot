"""Explicit native host provisioning. Windows owns one managed WSL2 distro.

No credential/identity is fabricated, imported or unlocked by Potbot. Native
keystore initialization/unlock is a separate private Syn terminal operation.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from potbot.domain.errors import CapabilityUnavailableError, StateMismatchError
from potbot.tools.temporary import create_directory

DISTRO = "Potbot-SynOperator"
UBUNTU = "Ubuntu-24.04"
OPERATOR_USER = "synoperator"
_PENDING = "registration-pending"
_HOST_PHASES = {
    "windows-platform",
    "wsl-registration",
    "native-prerequisites",
    "native-keystore",
}


class WslRestartRequired(CapabilityUnavailableError):
    """The Windows installer returned an explicit reboot result."""


@dataclass(frozen=True)
class WslRegistration:
    identifier: str
    location: Path
    version: int
    state: int = 1


def _registered_distro() -> WslRegistration | None:
    """Read the actual per-user registration, including its storage and identity."""
    winreg = import_module("winreg")

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Lxss"
        )
    except FileNotFoundError:
        return None
    matches = []
    with key:
        for index in range(winreg.QueryInfoKey(key)[0]):
            name = winreg.EnumKey(key, index)
            with winreg.OpenKey(key, name) as entry:
                try:
                    distro, _ = winreg.QueryValueEx(entry, "DistributionName")
                except FileNotFoundError:
                    continue
                if not isinstance(distro, str) or distro.casefold() != DISTRO.casefold():
                    continue
                location, _ = winreg.QueryValueEx(entry, "BasePath")
                version, _ = winreg.QueryValueEx(entry, "Version")
                state, _ = winreg.QueryValueEx(entry, "State")
                if (
                    not isinstance(location, str)
                    or not Path(location).is_absolute()
                    or type(version) is not int
                    or type(state) is not int
                ):
                    raise StateMismatchError("Managed WSL registration is invalid")
                matches.append(WslRegistration(UUID(name).hex, Path(location), version, state))
    if len(matches) > 1:
        raise StateMismatchError("Managed WSL registration is ambiguous")
    return next(iter(matches), None)


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

_ACCOUNTING_PROBE = """
import gzip, json, os

required = ("CONFIG_TASKSTATS", "CONFIG_TASK_XACCT", "CONFIG_TASK_IO_ACCOUNTING")
try:
    with gzip.open("/proc/config.gz", "rt", encoding="utf-8") as config:
        text = config.read(2 * 1024 * 1024 + 1)
    if len(text) > 2 * 1024 * 1024:
        raise ValueError
except (OSError, ValueError):
    result = {"accounting": "unknown", "missing_options": []}
else:
    settings = set(text.splitlines())
    missing = [option for option in required if option + "=y" not in settings]
    result = {"accounting": "unsupported" if missing else "available", "missing_options": missing}
print(json.dumps({**result, "kernel": os.uname().release}))
"""


def _decode(raw: bytes) -> str:
    return raw.decode("utf-16-le" if b"\0" in raw else "utf-8", errors="replace").lstrip("\ufeff")


def windows_environment() -> dict[str, str]:
    names = (
        "SystemRoot",
        "WINDIR",
        "SystemDrive",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "TEMP",
        "TMP",
    )
    environment = {name: os.environ[name] for name in names if name in os.environ}
    environment["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    return environment


def run_host(argv: list[str], *, timeout: int = 30) -> str:
    # Output goes to a bounded caller-controlled process, never a hidden shell.
    # Provisioning has a fixed wall-clock cap and cannot unregister a distro.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=flags,
            env=windows_environment() if os.name == "nt" else None,
        )
    except subprocess.TimeoutExpired:
        raise CapabilityUnavailableError(
            "Native host setup timed out; rerun host-setup to check the retained state"
        ) from None
    if len(result.stdout) + len(result.stderr) > 4 * 1024 * 1024:
        raise StateMismatchError("Native setup diagnostics exceeded their bound")
    # Accept Win32 reboot results and their HRESULT forms without parsing
    # localized WSL output. A successful process may also print reboot advice.
    if argv[1:2] == ["--install"] and result.returncode & 0xFFFFFFFF in {
        1641,
        3010,
        0x80070669,
        0x80070BC2,
    }:
        raise WslRestartRequired(
            "Windows reported a restart is required; restart, then rerun host-setup."
        )
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
        self.location = self.root / "distro"

    @contextmanager
    def _setup_lock(self) -> Iterator[None]:
        path = self.root / ".setup.lock"
        if path.is_symlink() or path.resolve() != path:
            raise StateMismatchError("Managed WSL setup lock requires a local file")
        with path.open("a+b") as lock:
            lock.seek(0)
            try:
                if os.name == "nt":
                    windows_lock = import_module("msvcrt")
                    windows_lock.locking(lock.fileno(), windows_lock.LK_NBLCK, 1)
                else:
                    posix_lock = import_module("fcntl")
                    posix_lock.flock(lock.fileno(), posix_lock.LOCK_EX | posix_lock.LOCK_NB)
            except OSError:
                raise StateMismatchError("Another managed WSL setup is in progress") from None
            yield

    def _read_receipt(self) -> dict[str, Any]:
        if self.receipt.is_symlink() or self.receipt.resolve() != self.receipt:
            raise StateMismatchError("Managed WSL ownership receipt requires a local file")
        record = json.loads(self.receipt.read_text(encoding="utf-8"))
        if (
            not isinstance(record, dict)
            or record.get("profile") != "potbot.syn-wsl-host/1"
            or record.get("distro") != DISTRO
            or record.get("distribution") != UBUNTU
            or record.get("location") != str(self.location)
        ):
            raise StateMismatchError("Managed WSL ownership receipt is invalid")
        if (
            record.get("state") not in (_PENDING, "installing", "ready")
            or record.get("user") != OPERATOR_USER
        ):
            raise StateMismatchError("Managed WSL lifecycle/owner is invalid")
        phase = record.get("setup_phase")
        pending = record.get("pending_controller_sha256")
        if (
            (phase is not None and (not isinstance(phase, str) or phase not in _HOST_PHASES))
            or type(record.get("restart_required", False)) is not bool
            or (
                pending is not None
                and (not isinstance(pending, str) or re.fullmatch(r"[0-9a-f]{64}", pending) is None)
            )
        ):
            raise StateMismatchError("Managed WSL setup checkpoint is invalid")
        return record

    def _write_receipt(self, record: dict[str, Any]) -> None:
        temporary = self.root / (uuid4().hex + ".json")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                output.write(json.dumps(record, sort_keys=True) + "\n")
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.receipt)
        finally:
            temporary.unlink(missing_ok=True)

    def _require_registration(
        self, registration: WslRegistration | None, record: dict[str, Any]
    ) -> WslRegistration:
        if registration is None:
            raise StateMismatchError("Registered operator distro is missing; no silent replacement")
        if (
            registration.location.resolve() != self.location
            or self.location.resolve() != self.location
            or record.get("registration_id", registration.identifier) != registration.identifier
        ):
            raise StateMismatchError("Managed WSL registration differs from the ownership receipt")
        if registration.version != 2:
            raise StateMismatchError("Managed operator requires a WSL2 registration")
        if registration.state != 1:
            raise CapabilityUnavailableError(
                "Managed WSL registration is not settled; retry host-setup"
            )
        return registration

    @contextmanager
    def _setup_session(self) -> Iterator[None]:
        create_directory(self.root)
        with self._setup_lock():
            yield

    def setup(self) -> dict[str, Any]:
        with self._setup_session():
            return self._setup()

    def _checkpoint(self, record: dict[str, Any], phase: str, **fields: Any) -> dict[str, Any]:
        updated = {**record, **fields, "setup_phase": phase}
        updated.pop("restart_required", None)
        if updated != record:
            self._write_receipt(updated)
        return updated

    def _install(self, args: list[str], record: dict[str, Any]) -> None:
        try:
            run_host([self.wsl, "--install", *args], timeout=1800)
        except WslRestartRequired:
            self._write_receipt({**record, "restart_required": True})
            raise

    def _setup(
        self, *, controller_sha256: str | None = None, replace_pending: bool = False
    ) -> dict[str, Any]:
        registration = _registered_distro()
        if self.receipt.exists() or self.receipt.is_symlink():
            record = self._read_receipt()
        else:
            if registration is not None:
                raise StateMismatchError("Existing named WSL distro has no ownership receipt")
            record = {
                "profile": "potbot.syn-wsl-host/1",
                "installation_id": uuid4().hex,
                "distro": DISTRO,
                "distribution": UBUNTU,
                "user": OPERATOR_USER,
                "state": _PENDING,
                "location": str(self.location),
            }
            self._write_receipt(record)
        pending = record.get("pending_controller_sha256")
        if controller_sha256 is not None:
            if pending is not None and pending != controller_sha256 and not replace_pending:
                raise StateMismatchError(
                    "Resume with the pending controller digest or explicitly replace the pending selection"
                )
            selected = record.get("controller")
            selected_sha256 = selected.get("wheel_sha256") if isinstance(selected, dict) else None
            if pending != controller_sha256 and (
                pending is not None or selected_sha256 != controller_sha256
            ):
                record = {**record, "pending_controller_sha256": controller_sha256}
                self._write_receipt(record)
        if registration is None and record["state"] == _PENDING and "registration_id" not in record:
            if self.location.exists() or self.location.is_symlink():
                raise StateMismatchError("Unregistered managed WSL storage needs reconciliation")
            try:
                versions = run_host([self.wsl, "--version"])
            except CapabilityUnavailableError:
                versions = ""
            if not re.search(r"\b\d+\.\d+\.\d+(?:\.\d+)?\s*$", versions, re.MULTILINE):
                record = self._checkpoint(record, "windows-platform")
                self._install(["--no-distribution", "--web-download"], record)
                raise CapabilityUnavailableError(
                    "WSL platform setup ran. Complete any requested Windows restart, "
                    "then rerun host-setup."
                )
            record = self._checkpoint(record, "wsl-registration")
            self._install(
                [
                    "--distribution",
                    UBUNTU,
                    "--name",
                    DISTRO,
                    "--location",
                    str(self.location),
                    "--version",
                    "2",
                    "--no-launch",
                    "--web-download",
                ],
                record,
            )
            registration = _registered_distro()
            if registration is None:
                raise CapabilityUnavailableError(
                    "WSL has not registered the operator distro. Complete any requested "
                    "Windows restart, then rerun host-setup."
                )
        registration = self._require_registration(registration, record)
        prerequisites_complete = record.get("prerequisite_revision") == 3 and (
            record["state"] == "ready"
            or (record["state"] == "installing" and record.get("setup_phase") == "native-keystore")
        )
        if not prerequisites_complete:
            record = self._checkpoint(
                record,
                "native-prerequisites",
                state="installing",
                registration_id=registration.identifier,
            )
            confirmation = run_host(
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
            if confirmation.splitlines()[-1:] != ["syn-native-prerequisites-ready"]:
                raise CapabilityUnavailableError("Native prerequisites did not confirm completion")
            record = self._checkpoint(record, "native-keystore", prerequisite_revision=3)
        self.start_keystore()
        ready = {
            **record,
            "state": "ready",
            "registration_id": registration.identifier,
            "prerequisite_revision": 3,
            "custody": "linux-secret-service",
            "keystore_unlocked": False,
            "operator_identity_created": False,
        }
        ready.pop("setup_phase", None)
        ready.pop("restart_required", None)
        if ready != record:
            self._write_receipt(ready)
        return ready

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
        record = self._read_receipt()
        if record["state"] != "ready":
            raise StateMismatchError("Managed WSL host is not ready")
        self._require_registration(_registered_distro(), record)
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

    def status(self) -> dict[str, Any]:
        """Report prerequisites without creating a controller or authorizing workers."""
        result: dict[str, Any] = {
            "profile": "potbot.syn-wsl-status/1",
            "host_state": "absent",
            "worker_qualification": "required",
        }
        if not (self.receipt.exists() or self.receipt.is_symlink()):
            if _registered_distro() is not None:
                raise StateMismatchError("Existing named WSL distro has no ownership receipt")
            return result
        record = self._read_receipt()
        result["host_state"] = record["state"]
        if phase := record.get("setup_phase"):
            result["setup_phase"] = phase
        if record.get("restart_required"):
            result["restart_required"] = True
        if pending := record.get("pending_controller_sha256"):
            result["pending_controller_sha256"] = pending
            if record["state"] == "ready":
                result["setup_phase"] = "controller-install"
        if isinstance(selected := record.get("controller"), dict):
            result["controller_sha256"] = selected.get("wheel_sha256")
        if record["state"] == "ready":
            output = run_host(
                self.command("/usr/bin/python3.12", ["-I", "-B", "-c", _ACCOUNTING_PROBE])
            )
            result["worker_prerequisites"] = json.loads(output)
        return result


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
