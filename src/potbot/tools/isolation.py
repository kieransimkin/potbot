"""Fail-closed Linux bubblewrap and seccomp launch construction.

Only the candidate is writable. System runtimes and explicitly selected assets
are read-only, the root is private, PID/network/user namespaces are private,
network and namespace/ptrace escape syscalls are denied, and capabilities are
dropped. Resource limits are per-process; the wall deadline covers the entire
namespace. Windows/macOS need a separately qualified backend.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError
from potbot.domain.serialization import content_hash

_DENIED_SYSCALLS = (
    "socket",
    "socketpair",
    "connect",
    "bind",
    "listen",
    "accept",
    "accept4",
    "sendto",
    "sendmsg",
    "sendmmsg",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
    "mount",
    "umount2",
    "pivot_root",
    "setns",
    "unshare",
    "bpf",
    "keyctl",
    "add_key",
    "request_key",
    "open_by_handle_at",
    "name_to_handle_at",
    "fsopen",
    "fsconfig",
    "fsmount",
    "move_mount",
    "open_tree",
    "mount_setattr",
    "pidfd_getfd",
    "perf_event_open",
    "userfaultfd",
)
_NAMESPACE_CLONE_FLAGS = (
    0x00020000,
    0x02000000,
    0x04000000,
    0x08000000,
    0x10000000,
    0x20000000,
    0x40000000,
)


class _ArgumentComparison(ctypes.Structure):
    _fields_ = [
        ("arg", ctypes.c_uint),
        ("op", ctypes.c_uint),
        ("datum_a", ctypes.c_uint64),
        ("datum_b", ctypes.c_uint64),
    ]


def runtime_roots() -> tuple[Path, ...]:
    """An explicit system-runtime allowlist, never the parent of a repository."""
    roots: list[Path] = []
    for value in ("/usr", "/bin", "/lib", "/lib64", sys.base_prefix, sys.prefix):
        path = Path(value).absolute()
        if path.exists() and path not in roots:
            roots.append(path)
    return tuple(roots)


def _seccomp_fd() -> int:
    library = ctypes.util.find_library("seccomp")
    if library is None or not hasattr(os, "memfd_create"):
        raise CapabilityUnavailableError("libseccomp and Linux memfd support are required")
    lib = ctypes.CDLL(library, use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_rule_add.restype = ctypes.c_int
    lib.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(_ArgumentComparison),
    ]
    lib.seccomp_rule_add_array.restype = ctypes.c_int
    lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.seccomp_export_bpf.restype = ctypes.c_int
    context = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW; unknown architectures are killed.
    if not context:
        raise CapabilityUnavailableError("seccomp policy could not be created")
    fd = -1
    try:
        for name in _DENIED_SYSCALLS:
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and lib.seccomp_rule_add(context, 0x00050001, number, 0) != 0:
                raise CapabilityUnavailableError("seccomp policy could not be compiled")
        # clone3 carries flags behind a pointer, which classic seccomp cannot
        # inspect. ENOSYS makes supported libc runtimes fall back to clone.
        clone3 = lib.seccomp_syscall_resolve_name(b"clone3")
        if clone3 >= 0 and lib.seccomp_rule_add(context, 0x00050026, clone3, 0) != 0:
            raise CapabilityUnavailableError("clone3 policy could not be compiled")
        clone = lib.seccomp_syscall_resolve_name(b"clone")
        if clone < 0:
            raise CapabilityUnavailableError("native clone policy is unavailable")
        for flag in _NAMESPACE_CLONE_FLAGS:
            comparison = _ArgumentComparison(0, 7, flag, flag)  # SCMP_CMP_MASKED_EQ
            if (
                lib.seccomp_rule_add_array(context, 0x00050001, clone, 1, ctypes.byref(comparison))
                != 0
            ):
                raise CapabilityUnavailableError("namespace clone policy could not be compiled")
        fd = os.memfd_create("potbot-seccomp", flags=os.MFD_CLOEXEC)
        if lib.seccomp_export_bpf(context, fd) != 0:
            raise CapabilityUnavailableError("seccomp policy could not be exported")
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise
    finally:
        lib.seccomp_release(context)


@dataclass(frozen=True, slots=True)
class IsolationLaunch:
    argv: tuple[str, ...]
    pass_fds: tuple[int, ...]
    identity: str


class BubblewrapBackend:
    """One qualified backend; absence or setup failure never launches on the host."""

    def __init__(self, executable: Path | None = None, prlimit: Path | None = None) -> None:
        self.executable = executable or Path(shutil.which("bwrap") or "/unavailable/bwrap")
        self.prlimit = prlimit or Path(shutil.which("prlimit") or "/unavailable/prlimit")

    def prepare(
        self,
        *,
        argv: tuple[str, ...],
        workspace_root: Path,
        cwd: Path,
        read_only_roots: tuple[Path, ...],
        writable_roots: tuple[Path, ...],
        sandbox_root: Path,
        status_fd: int,
        memory_bytes: int,
        cpu_seconds: int,
        max_file_bytes: int,
        max_open_files: int,
    ) -> IsolationLaunch:
        if (
            platform.system() != "Linux"
            or platform.machine() not in {"x86_64", "aarch64"}
            or not all(
                path.is_file() and os.access(path, os.X_OK)
                for path in (self.executable, self.prlimit)
            )
        ):
            raise CapabilityUnavailableError("qualified bubblewrap isolation is unavailable")
        root = workspace_root.resolve(strict=True)
        sandbox = sandbox_root.resolve(strict=True)
        work = cwd.resolve(strict=True)
        if not root.is_dir() or not work.is_relative_to(root) or not root.is_relative_to(sandbox):
            raise InvalidInputError("working directory is outside the registered workspace sandbox")
        roots: list[Path] = []
        for path in (*runtime_roots(), *read_only_roots):
            absolute = path.absolute()
            resolved = absolute.resolve(strict=True)
            if resolved == Path("/") or root.is_relative_to(resolved):
                raise InvalidInputError("read-only mounts must not expose a candidate ancestor")
            if not resolved.is_file() and not resolved.is_dir():
                raise InvalidInputError("read-only mount is not a regular asset")
            if absolute not in roots:
                roots.append(absolute)
        writable: list[Path] = []
        for path in writable_roots:
            resolved = path.resolve(strict=True)
            if not resolved.is_dir() or not resolved.is_relative_to(sandbox):
                raise InvalidInputError(
                    "writable mounts must be directories inside the workspace sandbox"
                )
            if resolved == root or root.is_relative_to(resolved) or resolved.is_relative_to(root):
                raise InvalidInputError(
                    "writable support mounts must be separate from the candidate tree"
                )
            if resolved not in writable:
                writable.append(resolved)
        seccomp = _seccomp_fd()
        try:
            arguments = [
                str(self.prlimit),
                f"--as={memory_bytes}",
                f"--cpu={cpu_seconds}",
                f"--fsize={max_file_bytes}",
                f"--nofile={max_open_files}",
                "--",
                str(self.executable),
                "--unshare-all",
                "--die-with-parent",
                "--new-session",
                "--cap-drop",
                "ALL",
                "--dir",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--dir",
                "/run",
                "--hostname",
                "potbot",
            ]
            arguments.extend(("--bind", str(root), str(root)))
            for path in writable:
                arguments.extend(("--bind", str(path), str(path)))
            for path in roots:
                arguments.extend(("--ro-bind", str(path), str(path)))
            arguments.extend(
                (
                    "--chdir",
                    str(work),
                    "--seccomp",
                    str(seccomp),
                    "--json-status-fd",
                    str(status_fd),
                    "--",
                )
            )
            arguments.extend(argv)
            identity = content_hash(
                {
                    "backend": "bubblewrap-seccomp-v1",
                    "binary_sha256": hashlib.sha256(self.executable.read_bytes()).hexdigest(),
                    "prlimit_sha256": hashlib.sha256(self.prlimit.read_bytes()).hexdigest(),
                    "roots": [str(p) for p in roots],
                    "writable_root": str(root),
                    "writable_support_roots": [str(p) for p in writable],
                    "sandbox_root": str(sandbox),
                    "network": "deny",
                    "denied_syscalls": _DENIED_SYSCALLS,
                    "namespace_clone_flags": _NAMESPACE_CLONE_FLAGS,
                    "clone3_policy": "enosys",
                    "memory_bytes_per_process": memory_bytes,
                    "cpu_seconds_per_process": cpu_seconds,
                    "file_bytes_per_file": max_file_bytes,
                    "open_files_per_process": max_open_files,
                }
            )
            return IsolationLaunch(tuple(arguments), (seccomp, status_fd), identity)
        except BaseException:
            os.close(seccomp)
            raise
