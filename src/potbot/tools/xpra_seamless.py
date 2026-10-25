"""Xpra-only seamless workspace display: Xpra owns the virtual Xorg/Xdummy.

A separate Xvfb server is never started or managed. The host must provision
Xpra >= 6.3 with its Xdummy backend. Only the private Unix protocol socket
is exposed to our authenticated gateway; it is never mounted in the sandbox.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path
from typing import BinaryIO


class XpraSessionError(RuntimeError):
    """Fail-closed Xpra lifecycle error. No display secrets in exception text."""


class XpraSeamlessSession:
    def __init__(self, root: Path, display: str, authority: Path, *,
                 binary: str = "xpra", startup_timeout: float = 20.0) -> None:
        if not root.is_absolute() or not root.is_dir() or root.is_symlink():
            raise ValueError("Xpra requires an existing absolute workspace root")
        if not display.startswith(":") or not display[1:].isdigit() or not 100 <= int(display[1:]) <= 999:
            raise ValueError("Xpra requires a private display number")
        if not authority.is_absolute() or authority.is_symlink():
            raise ValueError("Xpra authority must be absolute and private")
        if not binary or ("/" in binary and not Path(binary).is_absolute()):
            raise ValueError("Xpra executable must be trusted")
        if not 2 <= startup_timeout <= 60:
            raise ValueError("Xpra startup timeout out of bounds")
        self.root, self.display, self.authority = root, display, authority
        self.binary, self.startup_timeout = binary, startup_timeout
        suffix = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:16]
        self.host_runtime_root = Path(f"/tmp/potbot-xpra-u{os.getuid()}")
        private = self.host_runtime_root / suffix
        self.socket_dir = private / "sock"
        self.runtime_dir = private / "run"
        self.log_path = root / ".potbot" / "x11" / "xpra.log"
        self._process: subprocess.Popen[bytes] | None = None
        self._log: BinaryIO | None = None
        self._log_owned = False

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def x_socket(self) -> Path:
        return Path(f"/tmp/.X11-unix/X{self.display[1:]}")

    def command(self) -> tuple[str, ...]:
        return (
            self.binary, "seamless", self.display, "--backend=x11", "--use-display=no",
            "--daemon=no", "--attach=no", "--exit-with-client=no",
            "--start-new-commands=no", "--systemd-run=no", "--mdns=no",
            "--clipboard=no", "--printing=no", "--file-transfer=no",
            "--speaker=off", "--microphone=off", "--webcam=no",
            "--html=on", f"--socket-dir={self.socket_dir}",
        )

    def _environment(self) -> dict[str, str]:
        allowed = ("PATH", "LANG", "LC_ALL", "TERM")
        clean = {k: os.environ[k] for k in allowed if k in os.environ}
        clean.update({"HOME": str(self.root), "DISPLAY": self.display,
                      "XAUTHORITY": str(self.authority),
                      "XDG_RUNTIME_DIR": str(self.runtime_dir)})
        return clean

    def socket_files(self) -> tuple[Path, ...]:
        if self.socket_dir.is_symlink() or not self.socket_dir.is_dir():
            return ()
        return tuple(p for p in self.socket_dir.iterdir() if not p.is_symlink()
                     and stat.S_ISSOCK(p.lstat().st_mode))

    @property
    def protocol_socket(self) -> Path:
        sockets = self.socket_files()
        if not self.running or len(sockets) != 1:
            raise XpraSessionError("Xpra protocol socket is unavailable or ambiguous")
        return sockets[0]

    def _verify_no_tcp_listener(self) -> None:
        process = self._process
        if not self.running or process is None:
            raise XpraSessionError("Xpra unexpectedly terminated")
        try:
            inodes: set[str] = set()
            group = process.pid
            for entry in Path("/proc").iterdir():
                if not entry.name.isdecimal():
                    continue
                try:
                    if os.getpgid(int(entry.name)) != group:
                        continue
                    for fd in (entry / "fd").iterdir():
                        link = os.readlink(fd)
                        if link.startswith("socket:[") and link.endswith("]"):
                            inodes.add(link[8:-1])
                except (ProcessLookupError, PermissionError, FileNotFoundError):
                    continue
            for name in ("/proc/net/tcp", "/proc/net/tcp6"):
                with open(name, encoding="ascii") as stream:
                    next(stream)
                    for row in stream:
                        fields = row.split()
                        if len(fields) > 9 and fields[3] == "0A" and fields[9] in inodes:
                            raise XpraSessionError("Xpra opened a forbidden TCP listener")
        except XpraSessionError:
            raise
        except OSError:
            raise XpraSessionError("Xpra network policy could not be verified") from None

    def start(self) -> None:
        if self._process is not None:
            raise XpraSessionError("Xpra session already started")
        executable = shutil.which(self.binary)
        if executable is None:
            raise XpraSessionError("Xpra executable unavailable")
        for directory in (self.host_runtime_root, self.socket_dir.parent,
                          self.socket_dir, self.runtime_dir):
            if directory.is_symlink():
                raise XpraSessionError("symlinked Xpra runtime directory")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.stat().st_uid != os.getuid():
                raise XpraSessionError("Xpra runtime directory owner mismatch")
            os.chmod(directory, 0o700)
        if list(self.socket_dir.iterdir()) or self.log_path.exists() or self.log_path.is_symlink():
            raise XpraSessionError("stale Xpra session needs operator reconciliation")
        fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self._log = os.fdopen(fd, "wb")
        self._log_owned = True
        try:
            self._process = subprocess.Popen(
                (executable, *self.command()[1:]), cwd=self.root,
                env=self._environment(), stdin=subprocess.DEVNULL,
                stdout=self._log, stderr=subprocess.STDOUT, start_new_session=True,
            )
            deadline = time.monotonic() + self.startup_timeout
            while time.monotonic() < deadline:
                if not self.running:
                    raise XpraSessionError("Xpra exited before its display was ready")
                if len(self.socket_files()) == 1 and self.x_socket.is_socket():
                    self._verify_no_tcp_listener()
                    return
                time.sleep(.1)
            raise XpraSessionError("Xpra display and protocol socket startup timed out")
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)
        if self._log is not None:
            self._log.close()
            self._log = None
        if self._log_owned:
            # Preserve audit logs without confusing a later clean restart with
            # an abandoned live process. Unique archival names never overwrite.
            if self.log_path.is_file() and not self.log_path.is_symlink():
                archived = self.log_path.with_name(f"xpra-{time.time_ns()}.log")
                self.log_path.rename(archived)
            self._log_owned = False
        # The subprocess owns its X11 and protocol sockets. Never delete unknown
        # sockets or restart on top of an ambiguous leftover display.
        for folder in (self.socket_dir, self.runtime_dir, self.socket_dir.parent):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
