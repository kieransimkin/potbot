"""Read-only Git capture and bounded, verifiable repository materialisation.

These helpers inspect Git objects and copy bytes. They never execute repository
hooks, filters, build scripts or a shell. A snapshot is not an OS sandbox.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import selectors
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    StateMismatchError,
)
from potbot.domain.serialization import canonical_json, content_hash

MAX_FILES = 20_000
MAX_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def safe_relative_path(value: str) -> str:
    """Accept a portable literal repository path, never a glob or traversal."""
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 2048
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or value.startswith("/")
        or any(part in {"", ".", ".."} or part.lower() == ".git" for part in value.split("/"))
    ):
        raise InvalidInputError("Repository paths must be bounded literal relative paths.")
    return value


@dataclass(frozen=True, slots=True)
class SnapshotProfile:
    """Controller-selected additional inputs and exclusions; directories are recursive."""

    untracked_paths: tuple[str, ...] = ()
    excluded_paths: tuple[str, ...] = ()
    secret_refs: tuple[str, ...] = ()
    lock_paths: tuple[str, ...] = (
        "uv.lock",
        "poetry.lock",
        "requirements.txt",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.lock",
        "go.sum",
    )

    def __post_init__(self) -> None:
        for name in ("untracked_paths", "excluded_paths", "lock_paths"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or len(values) > 1024:
                raise InvalidInputError("Snapshot input lists must be bounded tuples.")
            for value in values:
                safe_relative_path(value)
            if len(set(values)) != len(values):
                raise InvalidInputError("Snapshot input lists cannot contain duplicate paths.")
        if not isinstance(self.secret_refs, tuple) or any(
            not isinstance(value, str) or not value.startswith("secret://") or len(value) > 512
            for value in self.secret_refs
        ):
            raise InvalidInputError("Excluded credentials require secret:// references.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "untracked_paths": list(self.untracked_paths),
            "excluded_paths": list(self.excluded_paths),
            "secret_refs": list(self.secret_refs),
            "lock_paths": list(self.lock_paths),
        }

    @classmethod
    def from_dict(cls, value: Any) -> SnapshotProfile:
        keys = {"untracked_paths", "excluded_paths", "secret_refs", "lock_paths"}
        if (
            not isinstance(value, dict)
            or set(value) != keys
            or any(not isinstance(item, list) for item in value.values())
        ):
            raise InvalidInputError("Invalid snapshot profile.")
        return cls(**{key: tuple(item) for key, item in value.items()})


def _excluded(path: str, exclusions: tuple[str, ...]) -> bool:
    return any(path == excluded or path.startswith(excluded + "/") for excluded in exclusions)


def _git(root: Path, *arguments: str, max_output: int = MAX_BYTES * 2) -> bytes:
    command = [
        "git",
        "--no-optional-locks",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "submodule.recurse=false",
        "-c",
        "protocol.allow=never",
        "-c",
        "core.attributesFile=/dev/null",
        "-C",
        str(root),
        *arguments,
    ]
    environment = {
        "PATH": os.defpath,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        process = subprocess.Popen(
            command,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        raise CapabilityUnavailableError("Git inspection is unavailable.") from None
    assert process.stdout is not None
    output = bytearray()
    deadline = time.monotonic() + 20
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise CapabilityUnavailableError("Git inspection exceeded its deadline.")
                for key, _ in selector.select(timeout=0.1):
                    chunk = os.read(key.fd, min(65_536, max_output + 1 - len(output)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > max_output:
                        raise InvalidInputError(
                            "Git inspection exceeded the snapshot output limit."
                        )
        result = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if result != 0:
            raise InvalidInputError(
                "Git could not inspect the selected local repository or revision."
            )
        return bytes(output)
    except BaseException as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        if isinstance(error, subprocess.TimeoutExpired):
            raise CapabilityUnavailableError("Git inspection exceeded its deadline.") from None
        raise
    finally:
        process.stdout.close()


def _path_text(raw: bytes) -> str:
    try:
        return safe_relative_path(raw.decode("utf-8"))
    except UnicodeError:
        raise CapabilityUnavailableError("Non-UTF-8 repository paths are unsupported.") from None


def _entries_digest(entries: list[dict[str, Any]]) -> str:
    return content_hash(
        [
            {key: entry[key] for key in ("path", "kind", "mode", "size", "sha256")}
            for entry in entries
        ]
    )


def _link_target(path: str, raw: bytes) -> str:
    try:
        target = raw.decode("utf-8")
    except UnicodeError:
        raise CapabilityUnavailableError("Non-UTF-8 symlinks are unsupported.") from None
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    if (
        not target
        or target.startswith("/")
        or "\\" in target
        or ":" in target
        or "\x00" in target
        or resolved == ".."
        or resolved.startswith("../")
        or any(part.lower() == ".git" for part in PurePosixPath(resolved).parts)
    ):
        raise CapabilityUnavailableError("Snapshot symlink escapes the execution tree.")
    return target


def _entry(path: str, mode: int, data: bytes, kind: str) -> dict[str, Any]:
    safe_relative_path(path)
    if len(data) > MAX_FILE_BYTES:
        raise InvalidInputError("An execution input exceeds the snapshot file limit.")
    if kind == "symlink":
        _link_target(path, data)
    return {
        "path": path,
        "kind": kind,
        "mode": mode,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "data": base64.b64encode(data).decode("ascii"),
    }


def _read_entry(root: Path, relative: str) -> dict[str, Any] | None:
    # Opening every directory without following links avoids a source symlink
    # redirecting a later tracked child into an unrelated host directory.
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    parts = safe_relative_path(relative).split("/")
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        metadata = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            data = os.readlink(parts[-1], dir_fd=descriptor).encode("utf-8")
            return _entry(relative, 0o777, data, "symlink")
        if not stat.S_ISREG(metadata.st_mode):
            raise CapabilityUnavailableError(
                "Execution inputs must be regular files or safe symlinks."
            )
        file_descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        with os.fdopen(file_descriptor, "rb") as stream:
            actual = os.fstat(stream.fileno())
            if not stat.S_ISREG(actual.st_mode) or actual.st_size > MAX_FILE_BYTES:
                raise InvalidInputError("Snapshot file changed type or exceeds the size limit.")
            data = stream.read(MAX_FILE_BYTES + 1)
        return _entry(relative, 0o755 if actual.st_mode & 0o111 else 0o644, data, "file")
    except FileNotFoundError:
        return None  # A tracked deletion is a reproducible input state.
    except OSError:
        raise CapabilityUnavailableError(
            "Repository input could not be read without link traversal."
        ) from None
    finally:
        os.close(descriptor)


def _walk_files(root: Path, relative: str = "") -> list[str]:
    current = root / relative
    if current.is_symlink():
        return [safe_relative_path(relative)]
    if not current.is_dir():
        if not current.exists():
            raise CapabilityUnavailableError("A declared execution input is unavailable.")
        return [safe_relative_path(relative)]
    found: list[str] = []
    for folder, directories, filenames in os.walk(current, followlinks=False):
        # Git control data can affect execution. It is neither silently copied
        # nor silently omitted from a declared directory/candidate snapshot.
        if any(name.lower() == ".git" for name in (*directories, *filenames)):
            raise CapabilityUnavailableError(
                "Nested Git control data requires an explicit repository adapter."
            )
        directories[:] = sorted(directories)
        for name in list(directories):
            path = Path(folder) / name
            if path.is_symlink():
                found.append(safe_relative_path(path.relative_to(root).as_posix()))
                directories.remove(name)
        for name in sorted(filenames):
            found.append(safe_relative_path((Path(folder) / name).relative_to(root).as_posix()))
        if len(found) > MAX_FILES:
            raise InvalidInputError("Snapshot contains too many execution inputs.")
    return found


def _validate_entries(entries: Any) -> list[dict[str, Any]]:
    if not isinstance(entries, list) or len(entries) > MAX_FILES:
        raise InvalidInputError("Invalid snapshot file inventory.")
    paths: list[str] = []
    total = 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "path",
            "kind",
            "mode",
            "size",
            "sha256",
            "data",
        }:
            raise InvalidInputError("Invalid snapshot file envelope.")
        path = safe_relative_path(entry["path"])
        if entry["kind"] not in {"file", "symlink"} or type(entry["mode"]) is not int:
            raise InvalidInputError("Invalid snapshot file kind or mode.")
        expected_modes = {0o644, 0o755} if entry["kind"] == "file" else {0o777}
        if entry["mode"] not in expected_modes or type(entry["size"]) is not int:
            raise InvalidInputError("Invalid snapshot permissions or size.")
        try:
            data = base64.b64decode(entry["data"], validate=True)
        except (TypeError, ValueError):
            raise InvalidInputError("Invalid snapshot byte encoding.") from None
        if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise InvalidInputError("Snapshot input digest does not match.")
        if len(data) > MAX_FILE_BYTES:
            raise InvalidInputError("Snapshot input exceeds its limit.")
        if entry["kind"] == "symlink":
            _link_target(path, data)
        total += len(data)
        paths.append(path)
    if total > MAX_BYTES or paths != sorted(set(paths)):
        raise InvalidInputError("Snapshot inventory must be sorted, unique and bounded.")
    path_set = set(paths)
    for path in paths:
        parents = PurePosixPath(path).parents
        if any(str(parent) in path_set for parent in parents if str(parent) != "."):
            raise InvalidInputError("Snapshot file paths overlap directories.")
    return entries


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """Canonical immutable serialization; properties return fresh detached values."""

    payload: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.payload, bytes) or len(self.payload) > MAX_BYTES * 2:
            raise InvalidInputError("Snapshot payload exceeds its bound.")
        try:
            value = json.loads(self.payload)
            if canonical_json(value).encode() != self.payload:
                raise InvalidInputError("Snapshot bytes must use their canonical encoding.")
            if not isinstance(value, dict) or set(value) != {
                "schema_version",
                "commit_id",
                "tree_id",
                "source_kind",
                "content_digest",
                "profile",
                "files",
                "submodules",
                "lock_identity",
            }:
                raise InvalidInputError("Invalid snapshot envelope.")
            if value["schema_version"] != 1 or type(value["schema_version"]) is not int:
                raise InvalidInputError("Unsupported snapshot version.")
            if not _OBJECT_ID.fullmatch(value["commit_id"]) or not _OBJECT_ID.fullmatch(
                value["tree_id"]
            ):
                raise InvalidInputError(
                    "Snapshot Git identities must be resolved object identifiers."
                )
            if value["source_kind"] not in {"committed", "working_tree", "candidate"}:
                raise InvalidInputError("Unknown snapshot source kind.")
            profile = SnapshotProfile.from_dict(value["profile"])
            entries = _validate_entries(value["files"])
            if value["content_digest"] != _entries_digest(entries):
                raise InvalidInputError("Snapshot content identity does not match.")
            if not isinstance(value["submodules"], dict) or any(
                not _OBJECT_ID.fullmatch(oid) for oid in value["submodules"].values()
            ):
                raise InvalidInputError("Invalid submodule references.")
            for path in value["submodules"]:
                safe_relative_path(path)
            locks = {
                entry["path"]: entry["sha256"]
                for entry in entries
                if entry["path"] in profile.lock_paths
            }
            if value["lock_identity"] != content_hash(locks):
                raise InvalidInputError("Dependency lock identity does not match.")
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
            raise InvalidInputError("Snapshot payload is malformed or inconsistent.") from None

    @property
    def manifest(self) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self.payload)
        value["files"] = [{k: v for k, v in item.items() if k != "data"} for item in value["files"]]
        value["reproducible"] = not value["submodules"]
        return value

    @property
    def content_digest(self) -> str:
        return str(self.manifest["content_digest"])

    @property
    def profile(self) -> SnapshotProfile:
        return SnapshotProfile.from_dict(self.manifest["profile"])


def _snapshot(
    entries: list[dict[str, Any]],
    commit: str,
    tree: str,
    kind: str,
    profile: SnapshotProfile,
    submodules: dict[str, str],
) -> RepositorySnapshot:
    entries.sort(key=lambda item: item["path"])
    return RepositorySnapshot(
        canonical_json(
            {
                "schema_version": 1,
                "commit_id": commit,
                "tree_id": tree,
                "source_kind": kind,
                "content_digest": _entries_digest(entries),
                "profile": profile.to_dict(),
                "files": entries,
                "submodules": submodules,
                "lock_identity": content_hash(
                    {
                        entry["path"]: entry["sha256"]
                        for entry in entries
                        if entry["path"] in profile.lock_paths
                    }
                ),
            }
        ).encode()
    )


def capture_snapshot(
    source_path: Path, *, profile: SnapshotProfile | None = None, committed_ref: str | None = None
) -> RepositorySnapshot:
    """Capture exact local bytes without modifying the source index or worktree.

    Uninitialised or unqualified submodules are retained as Git references, and
    materialisation is explicitly unavailable until their contents are captured
    by a future submodule adapter. No network acquisition is attempted.
    """
    selected = SnapshotProfile() if profile is None else profile
    if not isinstance(selected, SnapshotProfile):
        raise InvalidInputError("An explicit snapshot profile is required.")
    root = Path(source_path).resolve(strict=True)
    if _git(root, "rev-parse", "--show-toplevel").decode().strip() != str(root):
        raise InvalidInputError("Capture must name the repository root.")
    reference = "HEAD" if committed_ref is None else committed_ref
    if (
        not isinstance(reference, str)
        or not reference
        or len(reference) > 500
        or "\x00" in reference
    ):
        raise InvalidInputError("Invalid Git revision.")
    commit = (
        _git(root, "rev-parse", "--verify", "--end-of-options", reference + "^{commit}")
        .decode()
        .strip()
    )
    tree = (
        _git(root, "rev-parse", "--verify", "--end-of-options", commit + "^{tree}").decode().strip()
    )
    listed = _git(root, "ls-tree", "-rz", "--full-tree", commit)
    objects: list[tuple[str, str, str]] = []
    submodules: dict[str, str] = {}
    for item in listed.split(b"\0"):
        if not item:
            continue
        metadata, raw_path = item.split(b"\t", 1)
        mode, object_kind, oid = metadata.decode("ascii").split(" ")
        path = _path_text(raw_path)
        if _excluded(path, selected.excluded_paths):
            continue
        if object_kind == "commit":
            submodules[path] = oid
        else:
            objects.append((path, mode, oid))
    if committed_ref is not None:
        if selected.untracked_paths:
            raise InvalidInputError("Committed snapshots cannot include working-tree-only inputs.")
        entries = [
            _entry(
                path,
                0o777 if mode == "120000" else 0o755 if mode == "100755" else 0o644,
                _git(root, "cat-file", "blob", oid, max_output=MAX_FILE_BYTES),
                "symlink" if mode == "120000" else "file",
            )
            for path, mode, oid in objects
        ]
        return _snapshot(entries, commit, tree, "committed", selected, submodules)

    def scan() -> list[dict[str, Any]]:
        paths = {
            _path_text(path)
            for path in _git(root, "ls-files", "-z", "--cached").split(b"\0")
            if path
        }
        for additional in selected.untracked_paths:
            if not _excluded(additional, selected.excluded_paths):
                paths.update(_walk_files(root, additional))
        entries = []
        for path in sorted(paths):
            if path in submodules or _excluded(path, selected.excluded_paths):
                continue
            entry = _read_entry(root, path)
            if entry is not None:
                entries.append(entry)
        _validate_entries(entries)
        return entries

    first = scan()
    second = scan()
    current = _git(root, "rev-parse", "--verify", "HEAD").decode().strip()
    if first != second or current != commit:
        raise StateMismatchError("Repository inputs changed while the snapshot was being captured.")
    return _snapshot(first, commit, tree, "working_tree", selected, submodules)


def capture_candidate(path: Path, baseline: RepositorySnapshot) -> RepositorySnapshot:
    """Capture a directory owned by the workspace manager; it need not contain Git metadata."""
    root = Path(path)
    profile = baseline.profile
    if baseline.manifest["submodules"]:
        raise CapabilityUnavailableError("Submodule contents have no qualified materialisation.")

    def scan() -> list[dict[str, Any]]:
        entries = []
        for relative in sorted(_walk_files(root)):
            if not _excluded(relative, profile.excluded_paths):
                entry = _read_entry(root, relative)
                if entry is not None:
                    entries.append(entry)
        return entries

    first = scan()
    if first != scan():
        raise StateMismatchError("Candidate changed while its state was being captured.")
    manifest = baseline.manifest
    return _snapshot(first, manifest["commit_id"], manifest["tree_id"], "candidate", profile, {})


def workspace_fingerprint(path: Path, *, excluded_paths: tuple[str, ...] = ()) -> str:
    """Hash actual execution bytes/modes, excluding only declared non-input paths."""
    for excluded in excluded_paths:
        safe_relative_path(excluded)
    entries = []
    for relative in sorted(_walk_files(Path(path))):
        if not _excluded(relative, excluded_paths):
            entry = _read_entry(Path(path), relative)
            if entry is not None:
                entries.append(entry)
    return _entries_digest(_validate_entries(entries))


def materialize_snapshot(snapshot: RepositorySnapshot, destination: Path) -> None:
    """Create one empty execution directory, then verify every materialised input."""
    if not isinstance(snapshot, RepositorySnapshot):
        raise InvalidInputError("A validated snapshot is required.")
    value = json.loads(snapshot.payload)
    if value["submodules"]:
        raise CapabilityUnavailableError(
            "Submodule references are recorded but their contents are unavailable."
        )
    destination = Path(destination).absolute()
    if destination.parent.resolve() != destination.parent or destination.is_symlink():
        raise InvalidInputError("Materialisation destination cannot traverse a symlink.")
    destination.mkdir(mode=0o700, exist_ok=False)
    for entry in value["files"]:
        path = destination / entry["path"]
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = base64.b64decode(entry["data"], validate=True)
        if entry["kind"] == "symlink":
            os.symlink(_link_target(entry["path"], data), path)
        else:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, entry["mode"]
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            path.chmod(entry["mode"])
    if workspace_fingerprint(destination) != snapshot.content_digest:
        raise StateMismatchError("Materialised content does not match its immutable snapshot.")
