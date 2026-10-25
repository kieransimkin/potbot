"""Host-only, bounded readback of the complete /etc/ai inverse history.

This is an integrity/replay audit, not a signature and not a restore endpoint.
It reconstructs every historical snapshot backwards from head.json, validates
file bytes as well as their public hashes, and checks every readable diff.
No guest data is executed, extracted to disk, or published in the audit result.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.tools.ai_config_history import (
    _identity,
    _physical_directory,
    _relative,
    _render_reverse_diff,
    _scan,
    reconstruct_prior_snapshot,
)

_REVISION = re.compile(r"([0-9]{12})\.reverse\.(json|diff)\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ALLOWED_NAMES = frozenset({"head.json", ".monitor.lock"})


@dataclass(frozen=True, slots=True)
class AiHistoryAudit:
    """Public, content-free verification receipt for the checked history chain."""

    latest_sequence: int
    current_digest: str
    original_digest: str
    verified_revisions: int
    current_entries: int
    checked_archive_bytes: int
    matches_live_tree: bool | None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "potbot-ai-history-audit/v1",
            "verified": True,
            "latest_sequence": self.latest_sequence,
            "current_digest": self.current_digest,
            "original_digest": self.original_digest,
            "verified_revisions": self.verified_revisions,
            "current_entries": self.current_entries,
            "checked_archive_bytes": self.checked_archive_bytes,
            "matches_live_tree": self.matches_live_tree,
        }


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise InvalidInputError("AI history JSON contains duplicate object members")
        result[name] = value
    return result


def _read_regular(root: Path, name: str, limit: int) -> bytes:
    """Read one operator-owned file, with no leaf symlinks or changing inode."""
    if not isinstance(name, str) or not name or "/" in name or name in {".", ".."}:
        raise ScopeDeniedError("history file name is unsafe")
    fd = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ScopeDeniedError("AI history entry must be an unlinked regular file")
        if before.st_size > limit:
            raise CapabilityUnavailableError("AI history document exceeds the audit read limit")
        parts = bytearray()
        while len(parts) <= limit:
            block = os.read(fd, min(65536, limit - len(parts) + 1))
            if not block:
                break
            parts.extend(block)
        after = os.fstat(fd)
        if (len(parts) != before.st_size or len(parts) > limit
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise StateMismatchError("AI history file was altered during readback")
        on_disk = os.stat(root / name, follow_symlinks=False)
        if (on_disk.st_dev, on_disk.st_ino) != (before.st_dev, before.st_ino):
            raise StateMismatchError("AI history file was replaced during readback")
        return bytes(parts)
    finally:
        os.close(fd)


def _load_json(root: Path, name: str, limit: int) -> tuple[Any, int]:
    raw = _read_regular(root, name, limit)
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_pairs)
    except (ValueError, UnicodeError) as exc:
        raise InvalidInputError("AI history contains invalid or ambiguous JSON") from exc
    return document, len(raw)


def _validate_snapshot(snapshot: object, *, max_files: int,
                       max_bytes: int) -> dict[str, dict[str, Any]]:
    """Validate the actual decoded bytes, not merely the stored SHA-256 string."""
    if not isinstance(snapshot, dict) or len(snapshot) > max_files:
        raise CapabilityUnavailableError("AI history snapshot has an invalid entry count")
    total = 0
    for name, entry in snapshot.items():
        _relative(name)
        if not isinstance(entry, dict) or type(entry.get("mode")) is not int:
            raise InvalidInputError("AI history snapshot contains an invalid entry")
        if not 0 <= entry["mode"] <= 0o7777:
            raise InvalidInputError("AI history contains an invalid file mode")
        if entry.get("type") == "dir":
            if set(entry) != {"type", "mode"}:
                raise InvalidInputError("AI history directory entry has extra attributes")
        elif entry.get("type") == "file":
            if set(entry) != {"type", "mode", "sha256", "data_base64"}:
                raise InvalidInputError("AI history file entry has unexpected attributes")
            if not isinstance(entry["sha256"], str) or not _DIGEST.fullmatch(entry["sha256"]):
                raise InvalidInputError("AI history file digest is malformed")
            encoded = entry["data_base64"]
            if not isinstance(encoded, str) or len(encoded) > (max_bytes + 2) * 2:
                raise InvalidInputError("AI history payload size is invalid")
            try:
                payload = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise InvalidInputError("AI history contains invalid binary payload") from exc
            total += len(payload)
            if total > max_bytes:
                raise CapabilityUnavailableError("AI history snapshot exceeds its data budget")
            if (base64.b64encode(payload).decode("ascii") != encoded
                    or hashlib.sha256(payload).hexdigest() != entry["sha256"]):
                raise StateMismatchError("AI history file bytes do not match recorded digest")
        else:
            raise InvalidInputError("AI history contains an unsupported file type")
        parent = name.rpartition("/")[0]
        if parent and snapshot.get(parent, {}).get("type") != "dir":
            raise StateMismatchError("AI history file has a missing or invalid parent")
    return snapshot


def audit_ai_history(
    past_dir: Path,
    *,
    config_dir: Path | None = None,
    lock_held: bool = False,
    max_files: int = 20_000,
    max_snapshot_bytes: int = 256 * 1024 * 1024,
    max_document_bytes: int = 512 * 1024 * 1024,
    max_archive_bytes: int = 2 * 1024**3,
    max_revisions: int = 50_000,
) -> AiHistoryAudit:
    """Replay every reversal, verify its diff and optionally compare live /etc/ai.

    A standalone audit requires the monitor's exclusive lock to be free.
    ``lock_held`` is allowed ONLY for the monitor that already owns the lock;
    other controller callers must leave it false. Failures never rewrite history.
    """
    if (type(max_files) is not int or not 1 <= max_files <= 20_000
            or type(max_snapshot_bytes) is not int or max_snapshot_bytes <= 0
            or type(max_document_bytes) is not int or max_document_bytes <= 0
            or type(max_archive_bytes) is not int or max_archive_bytes <= 0
            or type(max_revisions) is not int or not 0 <= max_revisions <= 1_000_000):
        raise InvalidInputError("AI history audit limits are invalid")
    root = _physical_directory(past_dir)
    if root.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ScopeDeniedError("history audit requires an operator-private directory")
    lock_fd = -1
    if not lock_held:
        try:
            lock_fd = os.open(root / ".monitor.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, FileNotFoundError) as exc:
            if lock_fd >= 0:
                os.close(lock_fd)
            raise StateMismatchError("history audit requires a stopped/exclusively owned monitor") from exc
    try:
        names = os.listdir(root)
        sequences: dict[int, set[str]] = {}
        for name in names:
            if name in _ALLOWED_NAMES:
                continue
            match = _REVISION.fullmatch(name)
            if match is None:
                raise StateMismatchError("history contains an unrecognized or incomplete record")
            sequence = int(match[1])
            if sequence == 0:
                raise StateMismatchError("reversal sequence must start with revision 1")
            sequences.setdefault(sequence, set()).add(match[2])
        head, checked = _load_json(root, "head.json", max_document_bytes)
        if not isinstance(head, dict) or set(head) != {"schema", "sequence", "digest", "snapshot"}:
            raise InvalidInputError("AI history head has an invalid contract")
        if head["schema"] != "potbot-etc-ai-head/v1" or type(head["sequence"]) is not int:
            raise InvalidInputError("AI history head schema/sequence is invalid")
        number = head["sequence"]
        if number < 0 or number > max_revisions:
            raise CapabilityUnavailableError("AI history exceeds the configured revision limit")
        if set(sequences) != set(range(1, number + 1)) or any(
            v != {"json", "diff"} for v in sequences.values()
        ):
            raise StateMismatchError("AI history has missing or orphaned reverse records")
        current = _validate_snapshot(
            head["snapshot"], max_files=max_files, max_bytes=max_snapshot_bytes
        )
        if not isinstance(head["digest"], str) or _identity(current) != head["digest"]:
            raise StateMismatchError("AI history head identity differs from its data")
        original_entries = len(current)
        current_digest = head["digest"]
        for sequence in range(number, 0, -1):
            inverse, size = _load_json(root, f"{sequence:012d}.reverse.json", max_document_bytes)
            diff = _read_regular(root, f"{sequence:012d}.reverse.diff", max_document_bytes)
            checked += size + len(diff)
            if checked > max_archive_bytes:
                raise CapabilityUnavailableError("AI history audit exceeds its archive budget")
            if (not isinstance(inverse, dict) or inverse.get("sequence") != sequence
                    or type(inverse.get("sequence")) is not int):
                raise StateMismatchError("AI reverse record sequence is invalid")
            if not isinstance(inverse.get("changes"), list):
                raise InvalidInputError("AI history has malformed reversal changes")
            if _render_reverse_diff(inverse["changes"], sequence).encode("utf-8") != diff:
                raise StateMismatchError("AI reverse text diff does not match its inverse payload")
            prior = reconstruct_prior_snapshot(current, inverse)
            current = _validate_snapshot(
                prior, max_files=max_files, max_bytes=max_snapshot_bytes
            )
        original_digest = _identity(current)
        matches: bool | None = None
        if config_dir is not None:
            config = _physical_directory(config_dir)
            live = _scan(config, max_files=max_files, max_bytes=max_snapshot_bytes,
                         max_file_bytes=max_snapshot_bytes)
            matches = live == head["snapshot"]
            if not matches:
                raise StateMismatchError("live /etc/ai differs from the fully audited history head")
        return AiHistoryAudit(
            number, current_digest, original_digest, number,
            original_entries, checked, matches,
        )
    finally:
        if lock_fd >= 0:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
