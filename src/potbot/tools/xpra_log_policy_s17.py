"""S17: operator-private Xpra log custody, independent of every guest mount.

Normal guest/agent logs use the workspace's managed /var/logs/ai overlay.  Host
Xpra stdout can contain private internal details, so it is never placed under
shared /home or exposed through any guest, browser or public API.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4


_MAX_ACTIVE = 64 * 1024 * 1024
_ARCHIVE = re.compile(r"xpra-([0-9]{10,20})-([0-9a-f]{32})\.log\Z")
_INSTANCE = re.compile(r"[0-9a-f]{32}\Z")


class XpraLogError(RuntimeError):
    """Fail-closed log custody error; never includes private host paths."""


def _private_dir(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise XpraLogError("operator log directory is not qualified")
    try:
        info = path.stat(follow_symlinks=False)
        if (path.resolve(strict=True) != path or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise XpraLogError("operator log directory has unsafe permissions")
    except OSError as exc:
        raise XpraLogError("operator log directory is unavailable") from exc
    return path


def _document(path: Path, max_bytes: int = 2048) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise XpraLogError("operator log receipt is invalid")
    meta = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or (
        stat.S_IMODE(meta.st_mode) & 0o077) or not 0 < meta.st_size <= max_bytes:
        raise XpraLogError("operator log receipt is unqualified")
    try:
        def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
            out: dict[str, object] = {}
            for key, value in items:
                if key in out:
                    raise ValueError("duplicate receipt key")
                out[key] = value
            return out
        data = json.loads(path.read_bytes(), object_pairs_hook=pairs)
        if not isinstance(data, dict):
            raise ValueError("invalid JSON")
        return data
    except (OSError, ValueError, UnicodeError) as exc:
        raise XpraLogError("operator log receipt is corrupt") from exc


def _digest(path: Path, *, max_bytes: int = _MAX_ACTIVE) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise XpraLogError("Xpra log is not a regular file")
    meta = path.stat(follow_symlinks=False)
    if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
            or stat.S_IMODE(meta.st_mode) & 0o077 or meta.st_size > max_bytes):
        raise XpraLogError("Xpra log exceeds custody bounds")
    sha = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            sha.update(block)
    return sha.hexdigest(), meta.st_size


class XpraLogCustody:
    """One workspace/display's bounded operator-only log and immutable archives.

    Unrecognized active logs or incomplete archive receipts are *not* cleaned
    automatically.  Exclusive per-display Xpra session locking stays upstream.
    """

    def __init__(self, operator_root: Path, display_root: Path) -> None:
        base = _private_dir(Path(operator_root))
        guest = Path(display_root).resolve(strict=True)
        if not guest.is_dir() or base == guest or base.is_relative_to(guest) or guest.is_relative_to(base):
            raise XpraLogError("operator logs overlap the display's filesystem")
        identifier = hashlib.sha256(os.fsencode(guest)).hexdigest()[:32]
        self.operator_root = base
        self.instance_dir = base / identifier
        if self.instance_dir.is_symlink():
            raise XpraLogError("Xpra instance logs were redirected")
        self.instance_dir.mkdir(mode=0o700, exist_ok=True)
        _private_dir(self.instance_dir)
        self.live_path = self.instance_dir / "xpra.log"
        self._inode: tuple[int, int] | None = None
        self._started = False
        self._faulted = False

    @classmethod
    def from_environment(cls, display_root: Path) -> XpraLogCustody:
        # Operator service controls environment. Production root should be a
        # private subtree of /var/logs/ai on the Linux/WSL2 operator host.
        name = os.environ.get("POTBOT_XPRA_LOG_ROOT", "/var/logs/ai/operator")
        return cls(Path(name), display_root)

    def open(self) -> BinaryIO:
        if self._started or self._faulted:
            raise XpraLogError("Xpra log custody cannot be reused")
        _private_dir(self.instance_dir)
        # Do not let a previous crash's orphaned archive or tampered receipt
        # disappear behind a new session, even though its live filename is gone.
        inventory = inspect_xpra_logs(self.operator_root, limit=4096)
        selected = next((item for item in inventory["instances"]
                         if item["namespace"] == self.instance_dir.name), None)
        if (selected is None or selected["requiresReconciliation"]
                or self.live_path.exists() or self.live_path.is_symlink()):
            raise XpraLogError("stale Xpra log requires operator reconciliation")
        fd = os.open(self.live_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        stat_result = os.fstat(fd)
        self._inode = stat_result.st_dev, stat_result.st_ino
        self._started = True
        return os.fdopen(fd, "wb")

    @property
    def healthy(self) -> bool:
        if not self._started or self._faulted or self._inode is None:
            return False
        try:
            meta = self.live_path.stat(follow_symlinks=False)
            return (stat.S_ISREG(meta.st_mode) and (meta.st_dev, meta.st_ino) == self._inode
                    and meta.st_uid == os.getuid() and not meta.st_mode & 0o077
                    and meta.st_size <= _MAX_ACTIVE)
        except OSError:
            return False

    def close(self, output: BinaryIO) -> None:
        """Archive exactly the owned inode; preserve all records after a fault."""
        if not self._started or self._inode is None:
            raise XpraLogError("Xpra log was not opened by this owner")
        if self._faulted:
            raise XpraLogError("Xpra log custody already faulted")
        try:
            output.flush()
            os.fsync(output.fileno())
            output.close()
            if not self.healthy:
                raise XpraLogError("Xpra log changed or exceeded the byte limit")
            sha, size = _digest(self.live_path)
            archive = f"xpra-{time.time_ns()}-{uuid4().hex}.log"
            dest = self.instance_dir / archive
            if dest.exists() or dest.is_symlink():
                raise XpraLogError("Xpra archive already exists")
            os.rename(self.live_path, dest)
            receipt_path = self.instance_dir / (archive + ".json")
            record = {"schema": "potbot-xpra-log/v1", "archive": archive,
                      "sha256": sha, "size": size}
            encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
            fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as receipt:
                receipt.write(encoded)
                receipt.flush()
                os.fsync(receipt.fileno())
            dfd = os.open(self.instance_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
            self._started = False
        except BaseException:
            self._faulted = True
            try:
                output.close()
            except OSError:
                pass
            raise


def inspect_xpra_logs(operator_root: Path, *, limit: int = 256) -> dict[str, object]:
    """Read-only host recovery index; never returns paths, raw contents or secrets."""
    if type(limit) is not int or not 1 <= limit <= 4096:
        raise XpraLogError("invalid log inventory limit")
    root = _private_dir(Path(operator_root))
    result: list[dict[str, object]] = []
    for folder in sorted(root.iterdir(), key=lambda p: p.name):
        if len(result) >= limit:
            raise XpraLogError("operator log inventory bound exceeded")
        if not _INSTANCE.fullmatch(folder.name):
            raise XpraLogError("unexpected operator log namespace")
        _private_dir(folder)
        files = list(folder.iterdir())
        if len(files) > 1024:
            raise XpraLogError("operator log archive bound exceeded")
        live = "xpra.log" in {item.name for item in files}
        orphan = 0
        archived = 0
        known: set[str] = set()
        for entry in files:
            if entry.name == "xpra.log":
                _digest(entry)  # A leftover live log always needs reconciliation.
                continue
            if entry.name.endswith(".json"):
                archive = entry.name[:-5]
                if not _ARCHIVE.fullmatch(archive):
                    raise XpraLogError("unexpected archive receipt")
                receipt = _document(entry)
                if (set(receipt) != {"schema", "archive", "sha256", "size"}
                        or receipt["schema"] != "potbot-xpra-log/v1"
                        or receipt["archive"] != archive
                        or type(receipt["size"]) is not int or not 0 <= receipt["size"] <= _MAX_ACTIVE
                        or not isinstance(receipt["sha256"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", receipt["sha256"])):
                    raise XpraLogError("operator archive receipt is inconsistent")
                digest, size = _digest(folder / archive)
                if (digest != receipt["sha256"] or size != receipt["size"]):
                    raise XpraLogError("operator archive content differs from receipt")
                known.add(archive)
                archived += 1
            elif not _ARCHIVE.fullmatch(entry.name):
                raise XpraLogError("unexpected operator log file")
        orphan = sum(1 for p in files if _ARCHIVE.fullmatch(p.name) and p.name not in known)
        result.append({"namespace": folder.name, "archived": archived,
                       "orphanedArchives": orphan, "active": live,
                       "requiresReconciliation": live or orphan > 0})
    return {"schema": "potbot-xpra-logs-s17/v1", "instances": result,
            "requiresReconciliation": any(x["requiresReconciliation"] for x in result)}
