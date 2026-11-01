"""Workspace scratch storage with inherited Windows ACLs and bounded cleanup.

Native identity/vault files retain their separate custody and atomic-file policy.
Creation never rewrites an existing parent ACL. POSIX cleanup restores owner
permissions only on verified directories inside the disposable tree; it never
chmods regular files, follows symlinks, or grants group/other permissions.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import time
import warnings
from base64 import urlsafe_b64encode
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

_WINDOWS = os.name == "nt"
# Capture capabilities once: fault-injection wrappers must not change dispatch.
_POSIX_FD_CLEANUP = (
    os.name == "posix"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "fchmod")
    and {os.open, os.stat, os.unlink, os.rmdir} <= os.supports_dir_fd
    and os.scandir in os.supports_fd
)
_COMPONENT = re.compile(r"[A-Za-z0-9._-]{0,64}\Z")


def _unique_name() -> str:
    # Preserve all UUID entropy in 22 characters, leaving Windows path headroom.
    return urlsafe_b64encode(uuid4().bytes).decode("ascii").rstrip("=")


class TemporaryStorageError(OSError):
    """Scratch storage is unusable; permanent denials retain the owned tree."""


def create_directory(path: Path) -> Path:
    """Inherit the selected Windows parent ACL; retain POSIX private mode."""
    path.mkdir(mode=0o777 if _WINDOWS else 0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise TemporaryStorageError(f"Scratch root is not an ordinary directory: {path}")
    return path


def scratch_root(root: Path | None = None) -> Path:
    """Select an explicit, environment-selected or current-project scratch root."""
    if root is None:
        configured = os.environ.get("POTBOT_TEMP_ROOT")
        root = Path(configured) if configured else Path.cwd() / ".potbot" / "temporary"
    if not isinstance(root, Path):
        raise TypeError("Scratch root must be a Path")
    return create_directory(root.absolute())


def allocate_file(parent: Path, *, prefix: str = "potbot-", suffix: str = "") -> tuple[int, Path]:
    """Exclusively create a destination-local file; never retry access denials.

    Unlike Windows tempfile's permission-as-collision loop, only an actual name
    collision is retryable. This does not create or change the parent's ACL.
    Close the returned descriptor before replacing or reopening the file.
    """
    if not _COMPONENT.fullmatch(prefix) or not _COMPONENT.fullmatch(suffix):
        raise ValueError("Temporary names must be bounded path components")
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for _ in range(8):
        path = parent / (prefix + _unique_name() + suffix)
        try:
            return os.open(path, flags, 0o600), path
        except FileExistsError:
            continue
    raise FileExistsError(f"Temporary file name collisions at {parent}")


def _directory_identity(info: os.stat_result) -> tuple[int, int]:
    if not stat.S_ISDIR(info.st_mode):
        raise TemporaryStorageError("Scratch directory was replaced by a non-directory")
    return info.st_dev, info.st_ino


def _same_directory(info: os.stat_result, identity: tuple[int, int]) -> None:
    if _directory_identity(info) != identity:
        raise TemporaryStorageError("Scratch directory identity changed during cleanup")


def _remove_posix_tree(path: Path, expected_identity: tuple[int, int] | None) -> None:
    """Unlink through pinned descriptors, repairing owned directories only.

    Callers must stop writers before cleanup. Unreadable directories,
    ownership denials and cross-device subtrees are not permission workarounds.
    In particular, never use chmod(path) in a pathname-based rmtree callback:
    a symlink swap could otherwise change permissions outside the scratch tree.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

    def empty(directory: int, device: int) -> None:
        info = os.fstat(directory)
        if info.st_dev != device:
            raise TemporaryStorageError("Refusing a cross-device scratch subtree")
        mode = stat.S_IMODE(info.st_mode)
        if mode & stat.S_IRWXU != stat.S_IRWXU:
            if info.st_uid != os.geteuid():
                raise PermissionError("Refusing to repair a foreign-owned scratch directory")
            # Unlink requires parent-directory write/search permission, NOT a
            # writable file. Preserve read-only files and external hardlinks.
            os.fchmod(directory, mode | stat.S_IRWXU)
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    child_info = entry.stat(follow_symlinks=False)
                    if not stat.S_ISDIR(child_info.st_mode):
                        os.unlink(entry.name, dir_fd=directory)
                        continue
                    identity = _directory_identity(child_info)
                    child = os.open(entry.name, flags, dir_fd=directory)
                    try:
                        _same_directory(os.fstat(child), identity)
                        empty(child, device)
                        _same_directory(
                            os.stat(entry.name, dir_fd=directory, follow_symlinks=False), identity
                        )
                        os.rmdir(entry.name, dir_fd=directory)
                    finally:
                        os.close(child)
                except FileNotFoundError:
                    # A vanished descendant is already gone; other failures
                    # must propagate, leaving _cleaned false and naming the tree.
                    continue

    parent = os.open(path.parent, flags)
    try:
        directory = os.open(path.name, flags, dir_fd=parent)
        try:
            info = os.fstat(directory)
            identity = _directory_identity(info)
            if expected_identity is not None:
                _same_directory(info, expected_identity)
            empty(directory, info.st_dev)
            _same_directory(os.stat(path.name, dir_fd=parent, follow_symlinks=False), identity)
            os.rmdir(path.name, dir_fd=parent)
        finally:
            os.close(directory)
    finally:
        os.close(parent)


def _remove_tree(path: Path, *, expected_identity: tuple[int, int] | None = None) -> None:
    if not _WINDOWS and _POSIX_FD_CLEANUP:
        _remove_posix_tree(path, expected_identity)
        return
    # Preserve native Windows read-only-file handling and sharing-lock retries.
    # Platforms lacking descriptor APIs do not get a path-based POSIX chmod
    # fallback: ordinary cleanup can proceed, but access denials remain errors.
    if expected_identity is not None:
        _same_directory(path.lstat(), expected_identity)

    def readonly(function: object, name: str, error: BaseException) -> None:
        target = Path(name)
        if isinstance(error, FileNotFoundError):
            return
        if _WINDOWS and isinstance(error, PermissionError) and not target.is_symlink():
            mode = target.stat().st_mode
            if stat.S_ISREG(mode) and not mode & stat.S_IWRITE:
                target.chmod(mode | stat.S_IWRITE)
                os.unlink(target)
                return
        raise error

    shutil.rmtree(path, onexc=readonly)


class TemporaryDirectory:
    """Fresh owned scratch tree, including files opened by child processes.

    Windows mkdir(0700) replaces inherited sandbox ACEs on supported Python
    versions. Using the selected parent's existing ACL avoids that replacement.
    File/rename/read/delete preflight runs before the caller can write payloads.
    Cleanup retries transient locks; permanent denials retain a named directory.
    Stop child writers first. POSIX teardown may restore owner rwx on readable,
    owned directories in this exact allocation, without mutating file modes.
    """

    def __init__(
        self,
        suffix: str = "",
        prefix: str = "potbot-",
        dir: Path | str | None = None,
        *,
        ignore_cleanup_errors: bool = False,
    ) -> None:
        if (
            not _COMPONENT.fullmatch(prefix)
            or not _COMPONENT.fullmatch(suffix)
            or prefix in {".", ".."}
            or suffix in {".", ".."}
        ):
            raise ValueError("Temporary names must be bounded path components")
        self._ignore = ignore_cleanup_errors
        self._cleaned = False
        parent = Path(dir) if dir is not None else None
        try:
            parent = scratch_root(parent)
            self.path = parent / (prefix + _unique_name() + suffix)
            self.path.mkdir(mode=0o777 if _WINDOWS else 0o700)
            self._identity = _directory_identity(self.path.lstat())
        except OSError as error:
            raise TemporaryStorageError(
                f"Scratch directory allocation failed at {parent}; "
                "select an accessible POTBOT_TEMP_ROOT or use the host's approved execution scope"
            ) from error
        self.name = str(self.path)
        try:
            probe = self.path / ".access-probe"
            with probe.open("xb") as stream:
                stream.write(b"potbot-scratch-probe")
                stream.flush()
                os.fsync(stream.fileno())
            moved = probe.with_name(".access-probe-renamed")
            probe.replace(moved)
            if moved.read_bytes() != b"potbot-scratch-probe":
                raise OSError("Scratch readback failed")
            moved.unlink()
            child = self.path / ".directory-probe"
            child.mkdir()
            list(child.iterdir())
            child.rmdir()
        except OSError as error:
            try:
                self.cleanup()
            except OSError:
                pass
            raise TemporaryStorageError(
                f"Scratch create/read/rename/delete preflight failed: {self.path}; "
                "select an accessible POTBOT_TEMP_ROOT or use the host's approved execution scope"
            ) from error

    def __enter__(self) -> str:
        return self.name

    def environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """Route ordinary child-process tempfile APIs into this validated tree."""
        if self._cleaned or not self.path.is_dir() or self.path.is_symlink():
            raise TemporaryStorageError("Temporary process environment is no longer active")
        result = dict(os.environ if base is None else base)
        for name in ("TEMP", "TMP", "TMPDIR", "POTBOT_TEMP_ROOT"):
            result[name] = self.name
        return result

    def __exit__(self, kind: object, error: BaseException | None, traceback: object) -> None:
        try:
            self.cleanup()
        except TemporaryStorageError as cleanup_error:
            if error is None:
                raise
            error.add_note(str(cleanup_error))
            warnings.warn(str(cleanup_error), RuntimeWarning, stacklevel=2)

    def cleanup(self) -> None:
        if self._cleaned:
            return
        for attempt in range(6):
            try:
                _remove_tree(self.path, expected_identity=self._identity)
                self._cleaned = True
                return
            except FileNotFoundError:
                self._cleaned = True
                return
            except OSError as error:
                transient = _WINDOWS and getattr(error, "winerror", None) in {32, 33, 145}
                if transient and attempt < 5:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                message = f"Temporary directory retained after cleanup failure: {self.path}"
                if self._ignore:
                    warnings.warn(message, RuntimeWarning, stacklevel=2)
                    return
                raise TemporaryStorageError(message) from error
