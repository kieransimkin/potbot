"""S13: local, operator-owned health attachment for sandbox worker processes.

The Unix socket is a *read-only* live-attestation endpoint, not a process-control
RPC. It never carries secrets, arbitrary paths, commands, or filesystem contents.
The owning operator must remain the only writer of /etc/ai history and the
controller must place the socket outside every guest-visible mount.

Linux SO_PEERCRED and an owner-only directory/socket protect other OS users;
other hostile processes running as the SAME OS user are not isolated by this
transport. The OS sandbox and supervisor remain mandatory security boundaries.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import struct
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError

_SCHEMA = "potbot-operator-attachment/v1"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_EPOCH = re.compile(r"[0-9a-f]{32}\Z")
_MAX_WIRE = 2048


def _identity(workspace_id: UUID, image_digest: str, qualification_sha256: str) -> None:
    if not isinstance(workspace_id, UUID) or any(
        not isinstance(v, str) or not _DIGEST.fullmatch(v)
        for v in (image_digest, qualification_sha256)
    ):
        raise InvalidInputError("Operator attachment requires a pinned workspace identity")


def mount_fingerprint(config: Path, history: Path) -> str:
    """Bind a live operator to the two exact host-backed /etc/ai sources."""
    for candidate in (config, history):
        if candidate.is_symlink() or not candidate.is_dir():
            raise CapabilityUnavailableError("Operator AI mount source is unavailable")
    payload = json.dumps([str(config.resolve(strict=True)), str(history.resolve(strict=True))],
                         separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _peer_uid(conn: socket.socket) -> int:
    if not hasattr(socket, "SO_PEERCRED") or os.name != "posix":
        raise CapabilityUnavailableError("Operator attachment requires Linux peer credentials")
    payload = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, _gid = struct.unpack("3i", payload)
    if pid <= 0:
        raise ScopeDeniedError("Operator peer has no kernel identity")
    return uid


def _socket_path(path: Path, *, existing: bool) -> Path:
    selected = Path(path)
    if not selected.is_absolute() or len(os.fsencode(selected)) > 200:
        raise ScopeDeniedError("Operator socket must have an absolute bounded path")
    parent = selected.parent
    if (parent.is_symlink() or not parent.is_dir() or parent.resolve(strict=True) != parent):
        raise ScopeDeniedError("Operator socket directory must not be redirected")
    details = parent.stat(follow_symlinks=False)
    if details.st_uid != os.getuid() or details.st_mode & 0o077:
        raise ScopeDeniedError("Operator socket directory must be owned and private")
    if existing:
        try:
            entry = selected.lstat()
        except FileNotFoundError as exc:
            raise CapabilityUnavailableError("Operator socket is not available") from exc
        if (not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != os.getuid()
                or entry.st_mode & 0o077):
            raise ScopeDeniedError("Operator socket must be a private Unix socket")
    return selected


def _recv_line(conn: socket.socket) -> dict[str, Any]:
    data = bytearray()
    while len(data) <= _MAX_WIRE:
        chunk = conn.recv(min(512, _MAX_WIRE + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if b"\n" in chunk:
            break
    if len(data) > _MAX_WIRE or not data.endswith(b"\n") or data.count(b"\n") != 1:
        raise InvalidInputError("Operator protocol frame was incomplete or too large")
    try:
        value = json.loads(data[:-1].decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise InvalidInputError("Operator protocol frame is invalid") from exc
    if not isinstance(value, dict):
        raise InvalidInputError("Operator protocol expects an object")
    return value


def _write_line(conn: socket.socket, document: dict[str, Any]) -> None:
    payload = json.dumps(document, separators=(",", ":"), sort_keys=True).encode() + b"\n"
    if len(payload) > _MAX_WIRE:
        raise InvalidInputError("Operator status exceeds the wire bound")
    conn.sendall(payload)


class OperatorAttachmentServer:
    """One read-only local endpoint belonging to an active AiWorkspaceOperator.

    The caller MUST hold AiConfigHistoryMonitor's exclusive operator lock
    throughout this server's lifetime; the attachment isn't a substitute.
    """

    def __init__(
        self, *, socket_path: Path, workspace_id: UUID, image_digest: str,
        qualification_sha256: str, status: Callable[[], Mapping[str, object]],
        mounts_sha256: str = "0" * 64,
    ) -> None:
        _identity(workspace_id, image_digest, qualification_sha256)
        if not callable(status):
            raise InvalidInputError("Operator status must be supplied by the host owner")
        self.path = Path(socket_path)
        self.workspace_id = workspace_id
        self.image_digest = image_digest
        self.qualification_sha256 = qualification_sha256
        if not _DIGEST.fullmatch(mounts_sha256):
            raise InvalidInputError("Operator mount fingerprint must be a SHA-256 digest")
        self.status = status
        self.mounts_sha256 = mounts_sha256
        self.epoch = uuid4().hex
        self._listener: socket.socket | None = None
        self._inode: int | None = None
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> OperatorAttachmentServer:
        if self._listener is not None or self._stopping.is_set():
            raise InvalidInputError("Operator attachment cannot be started twice")
        _socket_path(self.path, existing=False)
        if self.path.is_symlink():
            raise ScopeDeniedError("Operator socket cannot be a symbolic link")
        if self.path.exists():
            current = self.path.lstat()
            if not stat.S_ISSOCK(current.st_mode) or current.st_uid != os.getuid():
                raise ScopeDeniedError("Unexpected object at operator socket")
            # Never remove a live operator endpoint. The owner holds the
            # /etc/ai singleton history lock before it reaches this code.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.15)
                try:
                    probe.connect(str(self.path))
                except (ConnectionRefusedError, FileNotFoundError):
                    if self.path.lstat().st_ino != current.st_ino:
                        raise ScopeDeniedError("Operator socket changed during recovery")
                    self.path.unlink()
                else:
                    raise ScopeDeniedError("Another operator is already listening")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            os.chmod(self.path, 0o600, follow_symlinks=False)
            listener.listen(8)
            listener.settimeout(0.2)
            self._inode = self.path.lstat().st_ino
            self._listener = listener
            self._thread = threading.Thread(target=self._serve, daemon=True,
                                            name="potbot-operator-attachment")
            self._thread.start()
            return self
        except BaseException:
            listener.close()
            if self.path.is_socket():
                self.path.unlink()
            raise

    def _serve(self) -> None:
        assert self._listener is not None
        while not self._stopping.is_set():
            try:
                peer, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with peer:
                peer.settimeout(0.4)
                try:
                    if _peer_uid(peer) != os.getuid():
                        continue
                    request = _recv_line(peer)
                    if request != {
                        "schema": _SCHEMA, "operation": "status",
                        "workspace_id": str(self.workspace_id),
                        "image_digest": self.image_digest,
                        "qualification_sha256": self.qualification_sha256,
                        "mounts_sha256": self.mounts_sha256,
                    }:
                        continue
                    state = self.status()
                    healthy = (state.get("running") is True
                               and state.get("history_healthy") is True
                               and state.get("workspace_id") == str(self.workspace_id)
                               and state.get("image_digest") == self.image_digest)
                    running = state.get("supervised_runs", 0)
                    if type(running) is not int or not 0 <= running <= 100_000:
                        healthy = False
                        running = 0
                    pending = state.get("pending_release_gates")
                    if pending is not None and (type(pending) is not int
                                               or not 0 <= pending <= 100_000):
                        healthy = False
                        pending = None
                    _write_line(peer, {
                        "schema": _SCHEMA, "workspace_id": str(self.workspace_id),
                        "image_digest": self.image_digest,
                        "qualification_sha256": self.qualification_sha256,
                        "mounts_sha256": self.mounts_sha256,
                        "epoch": self.epoch, "healthy": healthy,
                        "history_healthy": state.get("history_healthy") is True,
                        "supervised_runs": running,
                        "pending_release_gates": pending,
                    })
                except (ValueError, KeyError, OSError, TimeoutError, RuntimeError):
                    # A local peer never receives raw filesystem or monitor errors.
                    continue

    def close(self) -> None:
        if self._listener is None:
            return
        self._stopping.set()
        self._listener.close()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
        try:
            if self.path.lstat().st_ino == self._inode:
                self.path.unlink()
        except FileNotFoundError:
            pass
        self._listener = None

    def __enter__(self) -> OperatorAttachmentServer:
        return self.start()

    def __exit__(self, *_args: object) -> None:
        self.close()


class OperatorAttachmentClient:
    """Pinned live-operator check, with an epoch fence across daemon restarts."""

    def __init__(
        self, *, socket_path: Path, workspace_id: UUID, image_digest: str,
        qualification_sha256: str, timeout_seconds: float = 0.4,
        mounts_sha256: str = "0" * 64,
    ) -> None:
        _identity(workspace_id, image_digest, qualification_sha256)
        if (not isinstance(timeout_seconds, (int, float))
                or not 0.05 <= timeout_seconds <= 2.0):
            raise InvalidInputError("Operator attachment timeout is invalid")
        self.path = Path(socket_path)
        self.workspace_id = workspace_id
        self.image_digest = image_digest
        self.qualification_sha256 = qualification_sha256
        if not _DIGEST.fullmatch(mounts_sha256):
            raise InvalidInputError("Operator mount fingerprint must be a SHA-256 digest")
        self.mounts_sha256 = mounts_sha256
        self.timeout = float(timeout_seconds)
        self._epoch: str | None = None
        self._lock = threading.Lock()
        self._cached_until = 0.0
        self._cached: dict[str, Any] | None = None

    def require_healthy(self) -> dict[str, Any]:
        with self._lock:
            if self._cached is not None and time.monotonic() < self._cached_until:
                return dict(self._cached)
            try:
                _socket_path(self.path, existing=True)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                    conn.settimeout(self.timeout)
                    conn.connect(str(self.path))
                    if _peer_uid(conn) != os.getuid():
                        raise ScopeDeniedError("Operator peer is not the controller user")
                    _write_line(conn, {
                        "schema": _SCHEMA, "operation": "status",
                        "workspace_id": str(self.workspace_id),
                        "image_digest": self.image_digest,
                        "qualification_sha256": self.qualification_sha256,
                        "mounts_sha256": self.mounts_sha256,
                    })
                    response = _recv_line(conn)
                if (set(response) != {
                    "schema", "workspace_id", "image_digest", "qualification_sha256",
                    "epoch", "healthy", "history_healthy", "supervised_runs",
                    "mounts_sha256", "pending_release_gates",
                } or response["schema"] != _SCHEMA
                    or response["workspace_id"] != str(self.workspace_id)
                    or response["image_digest"] != self.image_digest
                    or response["qualification_sha256"] != self.qualification_sha256
                    or response["mounts_sha256"] != self.mounts_sha256
                    or not isinstance(response["epoch"], str)
                    or not _EPOCH.fullmatch(response["epoch"])
                    or response["healthy"] is not True
                    or response["history_healthy"] is not True
                    or type(response["supervised_runs"]) is not int
                    or not 0 <= response["supervised_runs"] <= 100_000
                    or not (response["pending_release_gates"] is None or
                            type(response["pending_release_gates"]) is int and
                            0 <= response["pending_release_gates"] <= 100_000)
                    or self._epoch is not None and self._epoch != response["epoch"]):
                    raise CapabilityUnavailableError("Operator identity or monitoring is unavailable")
                self._epoch = response["epoch"]
                self._cached = response
                self._cached_until = time.monotonic() + 0.15
                return dict(response)
            except (OSError, ValueError, TimeoutError, KeyError, RecursionError):
                self._cached = None
                self._cached_until = 0
                raise CapabilityUnavailableError("Operator attachment is unavailable") from None

    def healthy(self) -> bool:
        try:
            self.require_healthy()
            return True
        except (CapabilityUnavailableError, ScopeDeniedError):
            return False


def runtime_attachment(runtime: Any) -> OperatorAttachmentClient | None:
    """Use only operator-provisioned host environment; never guest-supplied env."""
    path = os.environ.get("POTBOT_OPERATOR_SOCKET")
    required = os.environ.get("POTBOT_OPERATOR_REQUIRED") == "1"
    if not path:
        if required:
            raise CapabilityUnavailableError("Managed workspace requires an operator socket")
        return None
    selected = Path(path)
    home = runtime.home.resolve(strict=True)
    rootfs = runtime.rootfs.resolve(strict=True)
    if any(selected == mount or selected.is_relative_to(mount)
           for mount in (home, rootfs)):
        raise ScopeDeniedError("Operator endpoint must stay outside guest mounts")
    if runtime.ai_config_dir is None or runtime.ai_history_dir is None:
        raise CapabilityUnavailableError("Managed workspace needs configured AI mounts")
    try:
        workspace_id = UUID(os.environ["POTBOT_OPERATOR_WORKSPACE_ID"])
    except (KeyError, ValueError) as exc:
        raise CapabilityUnavailableError("Managed workspace identity is unavailable") from exc
    return OperatorAttachmentClient(
        socket_path=selected, workspace_id=workspace_id,
        image_digest=runtime.image_digest,
        qualification_sha256=runtime.qualification_sha256,
        mounts_sha256=mount_fingerprint(runtime.ai_config_dir, runtime.ai_history_dir),
    )
