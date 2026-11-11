"""Install an explicitly digest-selected Potbot wheel in its managed WSL host."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from potbot.domain.errors import ScopeDeniedError, StateMismatchError

from .host import OPERATOR_USER, WslOperatorHost, run_host, windows_environment

ROOT = f"/home/{OPERATOR_USER}/.local/share/potbot/controllers"

# Fixed native code, never shell-interpolated. Syn itself still comes only
# from its independently selected signed official release.
INSTALL = r"""
import hashlib, json, os, pathlib, stat, subprocess, sys, venv, zipfile
wheel, expected, base, retained = sys.argv[1:]
path = pathlib.Path(wheel)
if path.stat().st_size > 50 * 1024 * 1024:
    raise SystemExit("Controller wheel exceeds bound")
if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
    raise SystemExit("Controller wheel digest differs")
with zipfile.ZipFile(path) as archive:
    entries = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
    if len(entries) != 1 or not entries[0].startswith("potbot-"):
        raise SystemExit("Controller must be a Potbot wheel")
    metadata = archive.read(entries[0]).decode()
    if "\nName: potbot\n" not in "\n" + metadata:
        raise SystemExit("Controller distribution identity differs")
root = pathlib.Path(base) / expected
if root.parent.resolve() != root.parent:
    raise SystemExit("Controller base path traverses a link")
if retained == "retained" and not root.is_dir():
    raise SystemExit("Selected controller environment is missing")
root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
root.mkdir(mode=0o700, exist_ok=True)
for directory in (root.parent, root):
    info = directory.lstat()
    if (directory.resolve() != directory or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise SystemExit("Controller directory is not private and operator-owned")
pending, ready = root / ".installing", root / ".ready"
marker = (expected + "\n").encode()
for entry in (pending, ready):
    if entry.is_symlink() or (entry.exists() and (
            not entry.is_file() or entry.stat().st_size != len(marker) or entry.read_bytes() != marker)):
        raise SystemExit("Controller installation marker differs")
environment = {"PATH": "/usr/bin:/bin", "HOME": str(pathlib.Path.home()), "LANG": "C.UTF-8"}
python = root / "venv/bin/python"
uv = root / "venv/bin/uv"
if not ready.exists() and retained != "retained":
    if not pending.exists():
        if any(root.iterdir()):
            raise SystemExit("Controller path already exists; explicit reconciliation required")
        with pending.open("xb") as output:
            output.write(marker)
            output.flush()
            os.fsync(output.fileno())
    if (root / "venv").resolve() != root / "venv":
        raise SystemExit("Pending controller venv has an unexpected path")
    venv.EnvBuilder(with_pip=True).create(root / "venv")
    subprocess.run([str(python), "-I", "-B", "-m", "pip", "--isolated", "--disable-pip-version-check",
                    "install", "--force-reinstall", "--no-input", "--only-binary", ":all:", str(path),
                    "cryptography==50.0.2", "uv==0.12.23"], env=environment, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=240)

verify = '''
import hashlib, importlib.metadata as meta, pathlib, sys, zipfile
wheel, expected, root = sys.argv[1:]
root = pathlib.Path(root)
if pathlib.Path(sys.prefix) != root / "venv":
    raise SystemExit("Controller interpreter is outside its selected environment")
with pathlib.Path(wheel).open("rb") as file:
    if hashlib.file_digest(file, "sha256").hexdigest() != expected:
        raise SystemExit("Controller wheel changed")
    file.seek(0)
    with zipfile.ZipFile(file) as archive:
        distribution = meta.distribution("potbot")
        for item in archive.infolist():
            if not item.filename.startswith("potbot/") or item.is_dir():
                continue
            installed = pathlib.Path(distribution.locate_file(item.filename))
            if (not installed.is_file() or installed.resolve() != installed
                    or not installed.is_relative_to(root / "venv")
                    or installed.read_bytes() != archive.read(item)):
                raise SystemExit("Installed controller payload differs from the wheel")
if meta.version("cryptography") != "50.0.2" or meta.version("uv") != "0.12.23":
    raise SystemExit("Installed controller dependency version differs")
'''
subprocess.run([str(python), "-I", "-B", "-c", verify, str(path), expected, str(root)],
               env=environment, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
if not uv.is_file() or not os.access(uv, os.X_OK):
    raise SystemExit("Installed controller uv launcher is unavailable")
subprocess.run([str(python), "-I", "-B", "-m", "potbot", "syn-operator", "--help"],
               env=environment, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
if pending.exists() and not ready.exists():
    pending.replace(ready)
print(json.dumps({"profile": "potbot.syn-wsl-controller/1", "wheel_sha256": expected,
                  "python": str(python), "uv": str(uv)}, sort_keys=True))
"""


def _controller_receipt(expected: str) -> dict[str, str]:
    return {
        "profile": "potbot.syn-wsl-controller/1",
        "wheel_sha256": expected,
        "python": f"{ROOT}/{expected}/venv/bin/python",
        "uv": f"{ROOT}/{expected}/venv/bin/uv",
    }


def install_controller(
    manager: WslOperatorHost, wheel: Path, expected: str, *, replace_pending: bool = False
) -> dict[str, str]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ScopeDeniedError("Controller requires an independently selected wheel SHA-256")
    if not wheel.is_absolute() or wheel.is_symlink() or wheel.suffix != ".whl":
        raise ScopeDeniedError("Controller requires an absolute reviewed wheel path")
    if (
        wheel.stat().st_size > 50 * 1024 * 1024
        or hashlib.sha256(wheel.read_bytes()).hexdigest() != expected
    ):
        raise StateMismatchError("Controller wheel differs from its selected digest")
    desired = _controller_receipt(expected)
    with manager._setup_session():
        record = manager._setup(controller_sha256=expected, replace_pending=replace_pending)
        retained = record.get("controller") == desired
        native = run_host(manager.command("/usr/bin/wslpath", ["-a", "-u", str(wheel)])).strip()
        if not native.startswith("/") or "\n" in native:
            raise StateMismatchError("Controller wheel has no canonical WSL path")
        result = json.loads(
            run_host(
                manager.command(
                    "/usr/bin/python3.12",
                    [
                        "-I",
                        "-B",
                        "-c",
                        INSTALL,
                        native,
                        expected,
                        ROOT,
                        "retained" if retained else "new",
                    ],
                ),
                timeout=340,
            )
        )
        if result != desired:
            raise StateMismatchError("Native controller installation receipt differs")
        if not retained or "pending_controller_sha256" in record:
            ready = {**record, "controller": desired}
            ready.pop("pending_controller_sha256", None)
            manager._write_receipt(ready)
    return desired


def forward(manager: WslOperatorHost, command: str, arguments: list[str]) -> int:
    import subprocess

    record = manager._read_receipt()
    controller = record.get("controller")
    if not isinstance(controller, dict):
        raise StateMismatchError("Install the exact reviewed native Potbot controller wheel first")
    selected = controller.get("wheel_sha256", "")
    if (
        not isinstance(selected, str)
        or re.fullmatch(r"[0-9a-f]{64}", selected) is None
        or controller != _controller_receipt(selected)
    ):
        raise StateMismatchError("Install the exact reviewed native Potbot controller wheel first")
    # Interactive unlock inherits the real terminal; Potbot never captures it.
    environment = windows_environment()
    # Only the acquisition controller may receive a repository access token.
    # Its installer strips this before launching official Syn or build tools.
    if command == "syn-release" and "POTBOT_SYN_GITHUB_TOKEN" in os.environ:
        environment["POTBOT_SYN_GITHUB_TOKEN"] = os.environ["POTBOT_SYN_GITHUB_TOKEN"]
        environment["WSLENV"] = "POTBOT_SYN_GITHUB_TOKEN"
    install_service = command == "syn-operator" and arguments and arguments[0] == "install-service"
    result = subprocess.run(
        manager.command(controller["python"], ["-I", "-B", "-m", "potbot", command, *arguments]),
        check=False,
        env=environment,
        stdout=subprocess.PIPE if install_service else None,
    )
    if install_service:
        if len(result.stdout) > 65536:
            raise StateMismatchError("Native user service response exceeded its bound")
        native = json.loads(result.stdout)
        if result.returncode == 0:
            from .windows_service import install_windows_service

            print(
                json.dumps(
                    {"native": native, "windows_lifetime": install_windows_service(manager)},
                    sort_keys=True,
                )
            )
        else:
            print(json.dumps(native, sort_keys=True))
    return result.returncode
