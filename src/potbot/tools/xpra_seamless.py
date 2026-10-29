"""Xpra-only seamless workspace display: Xpra owns the virtual Xorg/Xdummy.

A separate Xvfb server is never started or managed. The host must provision
Xpra >= 6.3 with its Xdummy backend. Only the private Unix protocol socket
is exposed to our authenticated gateway; it is never mounted in the sandbox.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, BinaryIO, Callable
from uuid import UUID, uuid4


class XpraSessionError(RuntimeError):
    """Fail-closed Xpra lifecycle error. No display secrets in exception text."""


class XpraSeamlessSession:
    def __init__(self, root: Path, display: str, authority: Path, *,
                 binary: str = "xpra", startup_timeout: float = 20.0,
                 readonly_mode: bool = False,
                 gui_audit_factory: Callable[[str, UUID], Any] | None = None) -> None:
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
        if type(readonly_mode) is not bool:
            raise ValueError("readonly Xpra mode must be set by the operator")
        self.readonly_mode = readonly_mode
        self.binary, self.startup_timeout = binary, startup_timeout
        self.gui_audit_factory = gui_audit_factory
        self._gui_audit: Any = None
        self._gui_audit_faulted = False
        suffix = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:16]
        self.host_runtime_root = Path(f"/tmp/potbot-xpra-u{os.getuid()}")
        private = self.host_runtime_root / suffix
        self.socket_dir = private / "sock"
        self.runtime_dir = private / "run"
        # Host Xpra output is never part of the guest's shared /home tree.
        # The operator provisions a private log root, normally /var/logs/ai/operator.
        self._log_custody = None
        if gui_audit_factory is not None:
            from potbot.tools.xpra_log_policy_s17 import XpraLogCustody

            self._log_custody = XpraLogCustody.from_environment(root)
            self.log_path = self._log_custody.live_path
        else:
            self.log_path = root / ".potbot" / "x11" / "xpra.log"
        self._process: subprocess.Popen[bytes] | None = None
        self._log: BinaryIO | None = None
        self._log_owned = False
        # Private socket identity previously observed from this object. Only a
        # cleanly stopped, inode-matching orphan can ever be reconciled on a
        # same-object restart; unrelated socket files remain a hard failure.
        self._last_owned_socket: tuple[Path, int, int] | None = None
        self._stopped_cleanly = False

    @property
    def running(self) -> bool:
        return (self._process is not None and self._process.poll() is None
                and (self._gui_audit is None or self._gui_audit.healthy)
                and (self._log_custody is None or self._log_custody.healthy))

    @property
    def x_socket(self) -> Path:
        return Path(f"/tmp/.X11-unix/X{self.display[1:]}")

    def command(self) -> tuple[str, ...]:
        return (
            self.binary, "seamless", self.display, "--backend=x11", "--use-display=no",
            "--daemon=no", "--attach=no", "--exit-with-client=no",
            "--start-new-commands=no", "--systemd-run=no", "--mdns=no",
            *(("--readonly=yes",) if self.readonly_mode else ()),
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
        if self._process is not None or self._gui_audit_faulted:
            raise XpraSessionError("Xpra session unavailable until operator reconciliation")
        executable = shutil.which(self.binary)
        if executable is None:
            raise XpraSessionError("Xpra executable unavailable")
        if self.gui_audit_factory is not None:
            expected = os.environ.get("POTBOT_XPRA_SHA256", "")
            candidate = Path(executable)
            if (not re.fullmatch(r"[0-9a-f]{64}", expected)
                    or candidate.is_symlink() or candidate.stat().st_mode & 0o022):
                raise XpraSessionError("host Xpra executable is not operator pinned")
            with candidate.open("rb") as src:
                if hashlib.file_digest(src, "sha256").hexdigest() != expected:
                    raise XpraSessionError("host Xpra executable differs from operator pin")
        for directory in (self.host_runtime_root, self.socket_dir.parent,
                          self.socket_dir, self.runtime_dir):
            if directory.is_symlink():
                raise XpraSessionError("symlinked Xpra runtime directory")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.stat().st_uid != os.getuid():
                raise XpraSessionError("Xpra runtime directory owner mismatch")
            os.chmod(directory, 0o700)
        if self._stopped_cleanly and self._last_owned_socket is not None:
            owned_path, device, inode = self._last_owned_socket
            if owned_path.parent == self.socket_dir and not owned_path.is_symlink():
                try:
                    metadata = owned_path.lstat()
                    if (metadata.st_uid == os.getuid() and stat.S_ISSOCK(metadata.st_mode)
                            and metadata.st_dev == device and metadata.st_ino == inode):
                        owned_path.unlink()
                except FileNotFoundError:
                    pass
        self._stopped_cleanly = False
        if list(self.socket_dir.iterdir()) or self.log_path.exists() or self.log_path.is_symlink():
            raise XpraSessionError("stale Xpra session needs operator reconciliation")
        if self._log_custody is not None:
            self._log = self._log_custody.open()
        else:
            fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            self._log = os.fdopen(fd, "wb")
        self._log_owned = True
        try:
            argv = (executable, *self.command()[1:])
            if self.gui_audit_factory is not None:
                # Principal attribution: the graphical display is operator
                # infrastructure, not an unverified agent's personal heat bar.
                identity = hashlib.sha256(repr(argv).encode("utf-8")).hexdigest()
                self._gui_audit = self.gui_audit_factory(identity, uuid4())
                argv = self._gui_audit.command(argv)
            self._process = subprocess.Popen(
                argv, cwd=self.root, env=self._environment(),
                stdin=subprocess.DEVNULL, stdout=self._log,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
            if self._gui_audit is not None:
                self._gui_audit.started(self._process.pid)
            deadline = time.monotonic() + self.startup_timeout
            while time.monotonic() < deadline:
                if not self.running:
                    raise XpraSessionError("Xpra exited before its display was ready")
                if len(self.socket_files()) == 1 and self.x_socket.is_socket():
                    self._verify_no_tcp_listener()
                    owned = self.socket_files()[0]
                    metadata = owned.lstat()
                    self._last_owned_socket = (owned, metadata.st_dev, metadata.st_ino)
                    return
                time.sleep(.1)
            raise XpraSessionError("Xpra display and protocol socket startup timed out")
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        process = self._process
        self._process = None
        audit, self._gui_audit = self._gui_audit, None
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
        if process is not None and process.poll() is not None:
            # Xpra may leave its AF_UNIX inode after a clean shutdown. Since
            # this owner launched and waited for the whole process group, it
            # may remove only the actual sockets inside its private socket
            # directory. Unknown files/symlinks remain fail-closed, and no
            # cleanup occurs for sessions we did not launch.
            for socket_file in self.socket_files():
                try:
                    metadata = socket_file.lstat()
                    if metadata.st_uid == os.getuid() and stat.S_ISSOCK(metadata.st_mode):
                        socket_file.unlink()
                except FileNotFoundError:
                    pass
        if self._log is not None:
            if self._log_custody is not None:
                try:
                    self._log_custody.close(self._log)
                except BaseException:
                    # Preserve the private log and mark the display indeterminate.
                    self._gui_audit_faulted = True
            else:
                self._log.close()
            self._log = None
        if self._log_owned:
            if self._log_custody is None:
                # Legacy standalone Xpra remains unchanged, but managed Xpra
                # never writes to the shared workspace home.
                if self.log_path.is_file() and not self.log_path.is_symlink():
                    archived = self.log_path.with_name(f"xpra-{time.time_ns()}.log")
                    self.log_path.rename(archived)
            self._log_owned = False
        # Remember successful termination for a later same-object restart, but
        # never treat unknown files or changed socket inodes as owned resources.
        audit_complete = True
        if audit is not None:
            try:
                audit_complete = bool(audit.finish(
                    confirmed_stopped=(process is not None and process.poll() is not None)))
            except Exception:
                audit_complete = False
        if audit is not None and not audit_complete:
            self._gui_audit_faulted = True
        self._stopped_cleanly = (process is not None and process.poll() is not None
                                 and audit_complete and not self._gui_audit_faulted)
        # The subprocess owns its X11 and protocol sockets. Never delete unknown
        # sockets or restart on top of an ambiguous leftover display.
        for folder in (self.socket_dir, self.runtime_dir, self.socket_dir.parent):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
