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
import hashlib, json, os, pathlib, subprocess, sys, venv, zipfile
wheel, expected, base = sys.argv[1:]
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
root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
if root.exists():
    raise SystemExit("Controller path already exists; explicit reconciliation required")
root.mkdir(mode=0o700)
environment = {"PATH": "/usr/bin:/bin", "HOME": str(pathlib.Path.home()), "LANG": "C.UTF-8"}
venv.EnvBuilder(with_pip=True).create(root / "venv")
python = root / "venv/bin/python"
subprocess.run([str(python), "-I", "-B", "-m", "pip", "--isolated", "--disable-pip-version-check",
                "install", "--no-input", "--only-binary", ":all:", str(path),
                "cryptography==50.0.2", "uv==0.12.23"], env=environment, check=True,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=240)
subprocess.run([str(python), "-I", "-B", "-m", "potbot", "syn-operator", "--help"],
               env=environment, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
print(json.dumps({"profile": "potbot.syn-wsl-controller/1", "wheel_sha256": expected,
                  "python": str(python), "uv": str(root / "venv/bin/uv")}, sort_keys=True))
"""


def install_controller(manager: WslOperatorHost, wheel: Path, expected: str) -> dict[str, str]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ScopeDeniedError("Controller requires an independently selected wheel SHA-256")
    if not wheel.is_absolute() or wheel.is_symlink() or wheel.suffix != ".whl":
        raise ScopeDeniedError("Controller requires an absolute reviewed wheel path")
    if (
        wheel.stat().st_size > 50 * 1024 * 1024
        or hashlib.sha256(wheel.read_bytes()).hexdigest() != expected
    ):
        raise StateMismatchError("Controller wheel differs from its selected digest")
    manager.setup()
    native = run_host(manager.command("/usr/bin/wslpath", ["-a", "-u", str(wheel)])).strip()
    if not native.startswith("/") or "\n" in native:
        raise StateMismatchError("Controller wheel has no canonical WSL path")
    result = json.loads(
        run_host(
            manager.command(
                "/usr/bin/python3.12", ["-I", "-B", "-c", INSTALL, native, expected, ROOT]
            ),
            timeout=300,
        )
    )
    if (
        result.get("profile") != "potbot.syn-wsl-controller/1"
        or result.get("wheel_sha256") != expected
        or result.get("python") != ROOT + "/" + expected + "/venv/bin/python"
    ):
        raise StateMismatchError("Native controller installation receipt differs")
    record = json.loads(manager.receipt.read_text())
    record["controller"] = result
    temporary = manager.receipt.with_suffix(".controller.tmp")
    with temporary.open("x", encoding="utf-8") as out:
        out.write(json.dumps(record, sort_keys=True) + "\n")
    temporary.replace(manager.receipt)
    return dict(result)


def forward(manager: WslOperatorHost, command: str, arguments: list[str]) -> int:
    import subprocess

    record = json.loads(manager.receipt.read_text())
    controller = record.get("controller", {})
    selected = controller.get("wheel_sha256", "")
    if (
        controller.get("profile") != "potbot.syn-wsl-controller/1"
        or re.fullmatch(r"[0-9a-f]{64}", selected) is None
        or controller.get("python") != ROOT + "/" + selected + "/venv/bin/python"
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
