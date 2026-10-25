"""Prepared, fail-closed Linux/WSL2 Bubblewrap launch from a verified system rootfs.

Host controller MUST qualify rootfs extraction/signature, directory ownership,
workspace image pin/lease and actual OS sandbox separately.  This builder does
not provision or execute an OCI image, and never falls back to an unconfined host.
"""

from __future__ import annotations

import configparser
import hashlib
import os
import platform
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import content_hash
from potbot.tools.ai_config_history import AiConfigHistoryMonitor
from potbot.tools.isolation import BubblewrapBackend, IsolationLaunch, _seccomp_fd
from potbot.tools.model_cache_routes import model_environment
from potbot.tools.operator_attachment_s13 import runtime_attachment
from potbot.tools.shared_tool_mounts import ToolWriteGrant


def is_wsl2(kernel_release: str) -> bool:
    value = kernel_release.casefold()
    return "microsoft" in value and "wsl2" in value


def wsl_guest_preflight(
    *, kernel_release: str, config_text: str | None, mountinfo: str,
) -> tuple[str, ...]:
    """WSL distribution checks, NOT proof of a Windows host security boundary."""
    lower = kernel_release.casefold()
    if "microsoft" not in lower:
        return ()  # native Linux; live bwrap/seccomp still required
    failures: list[str] = []
    if not is_wsl2(kernel_release):
        failures.append("WSL2 Linux kernel required")
    if config_text is None:
        failures.append("explicit /etc/wsl.conf required")
    else:
        cfg = configparser.ConfigParser(interpolation=None)
        try:
            cfg.read_string(config_text)
            for section, key in (
                ("automount", "enabled"),
                ("automount", "mountFsTab"),
                ("interop", "enabled"),
                ("interop", "appendWindowsPath"),
            ):
                if cfg.get(section, key, fallback="").strip().casefold() != "false":
                    failures.append(f"wsl.conf {section}.{key} must be false")
        except configparser.Error:
            failures.append("invalid wsl.conf")
    # DrvFs can still be mounted manually; reject it even with automount disabled.
    if any(" - drvfs " in line.casefold() or " - 9p " in line.casefold()
           for line in mountinfo.splitlines()):
        failures.append("Windows shared filesystem visible to WSL guest")
    return tuple(failures)


def _guest_cwd(home: Path, cwd: Path) -> str:
    try:
        relative = cwd.resolve(strict=True).relative_to(home)
    except (OSError, ValueError) as exc:
        raise ScopeDeniedError("working directory must stay within persistent /home") from exc
    return "/home" if relative == Path(".") else "/home/" + relative.as_posix()


@dataclass(frozen=True, slots=True)
class SharedWorkspaceRuntime:
    image_digest: str
    rootfs: Path
    home: Path
    qualification_sha256: str
    model_staging: Path | None = None
    shared_models: Path | None = None
    shared_tools: Path | None = None
    tool_entrypoints: tuple[tuple[str, str], ...] = ()
    ai_config_dir: Path | None = None
    ai_history_dir: Path | None = None

    def __post_init__(self) -> None:
        import re
        for digest in (self.image_digest, self.qualification_sha256):
            if not isinstance(digest, str) or not re.fullmatch("[a-f0-9]{64}", digest):
                raise InvalidInputError("runtime requires OCI and qualification hashes")


class SharedWorkspaceBubblewrapBackend(BubblewrapBackend):
    guest_executable = True  # Executable lives in the verified image, not on the host.
    """Drop-in ProcessSupervisor backend, restricted to a single pinned /home.

    The launcher requires a host-side admitted lease and verified image pin.
    It does not itself grant an agent authorization to execute anything.
    """

    def __init__(self, runtime: SharedWorkspaceRuntime, *,
                 executable: Path | None = None, prlimit: Path | None = None,
                 tool_write_grant: ToolWriteGrant | None = None,
                 ai_monitor: AiConfigHistoryMonitor | None = None) -> None:
        self.runtime = runtime
        self.tool_write_grant = tool_write_grant
        self.ai_monitor = ai_monitor
        # In a managed native service, workers attach to the operator's live
        # host-only socket; they MUST NOT instantiate another history watcher.
        self.operator_attachment = runtime_attachment(runtime)
        self.executable = executable or Path(shutil.which("bwrap") or "/unavailable/bwrap")
        self.prlimit = prlimit or Path(shutil.which("prlimit") or "/unavailable/prlimit")

    def operator_healthy(self) -> bool:
        attachment = self.operator_attachment
        return attachment is None or attachment.healthy()

    def prepare(
        self, *, argv: tuple[str, ...], workspace_root: Path, cwd: Path,
        read_only_roots: tuple[Path, ...], writable_roots: tuple[Path, ...],
        sandbox_root: Path, status_fd: int, memory_bytes: int, cpu_seconds: int,
        max_file_bytes: int, max_open_files: int, ipc_profile: str = "none",
        guest_uid: int | None = None, guest_gid: int | None = None,
    ) -> IsolationLaunch:
        if (guest_uid is None) != (guest_gid is None) or (
            guest_uid is not None and (
                type(guest_uid) is not int or not 420 <= guest_uid <= 65533
                or guest_gid != 69
            )
        ):
            raise InvalidInputError("sandbox guest identity must use allocated UID and GID 69")
        if ipc_profile != "none" or read_only_roots or writable_roots:
            raise ScopeDeniedError("shared workspace forbids extra writable/readable host mounts or IPC")
        # Admission precedes executable inspection, bind mounts and dispatch.
        if self.operator_attachment is not None:
            self.operator_attachment.require_healthy()
        if platform.system() != "Linux" or platform.machine() not in {"x86_64", "aarch64"}:
            raise CapabilityUnavailableError("shared sandbox requires a qualified Linux/WSL2 guest")
        if not all(p.is_file() and os.access(p, os.X_OK) for p in (self.executable, self.prlimit)):
            raise CapabilityUnavailableError("bwrap/prlimit missing in Linux guest")
        try:
            kernel = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").strip()
            wsl = Path("/etc/wsl.conf")
            config = wsl.read_text(encoding="utf-8") if wsl.exists() else None
            mounts = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        except OSError as exc:
            raise CapabilityUnavailableError("Linux guest feature preflight unavailable") from exc
        errors = wsl_guest_preflight(kernel_release=kernel, config_text=config, mountinfo=mounts)
        if errors:
            raise CapabilityUnavailableError("WSL2 isolation preflight failed: " + "; ".join(errors))
        try:
            rootfs = self.runtime.rootfs.resolve(strict=True)
            home = self.runtime.home.resolve(strict=True)
            original_home = Path(workspace_root).resolve(strict=True)
            sandbox = Path(sandbox_root).resolve(strict=True)
        except OSError as exc:
            raise CapabilityUnavailableError("provisioned workspace paths unavailable") from exc
        if (not rootfs.is_dir() or not home.is_dir() or home != original_home
                or sandbox != home or rootfs == home or rootfs.is_relative_to(home)
                or home.is_relative_to(rootfs)):
            raise ScopeDeniedError("shared workspace home/rootfs boundaries do not match")
        for target in ("home", "proc", "dev", "var/ai", "opt/potbot/models", "usr/local/tools"):
            path = rootfs / target
            if not path.is_dir() or path.is_symlink():
                raise CapabilityUnavailableError("system image lacks a safe mount destination")
        if (not (rootfs / "tmp").is_symlink()
                or os.readlink(rootfs / "tmp") != "/home/.tmp"):
            raise CapabilityUnavailableError("/tmp must map to persistent-home scratch")
        if not (home / ".tmp").is_dir() or (home / ".tmp").is_symlink():
            raise CapabilityUnavailableError("workspace /home/.tmp must be provisioned")
        if (self.runtime.model_staging is None or self.runtime.shared_models is None
                or self.runtime.shared_tools is None):
            raise CapabilityUnavailableError("sandbox requires staging, models and shared tools mounts")
        for candidate in (self.runtime.model_staging, self.runtime.shared_models,
                          self.runtime.shared_tools):
            if Path(candidate).is_symlink():
                raise ScopeDeniedError("sandbox model mount source cannot be a symlink")
        try:
            staging = self.runtime.model_staging.resolve(strict=True)
            library = self.runtime.shared_models.resolve(strict=True)
            tools = self.runtime.shared_tools.resolve(strict=True)
        except OSError as exc:
            raise CapabilityUnavailableError("sandbox model directories must be pre-provisioned") from exc
        if not staging.is_dir() or not library.is_dir() or not tools.is_dir():
            raise CapabilityUnavailableError("sandbox model sources must be directories")
        sources = (home, rootfs, staging, library, tools)
        if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
               for index, a in enumerate(sources) for b in sources[index + 1:]):
            raise ScopeDeniedError("sandbox home, image, staging and global model store must be disjoint")
        # Modern images advertise both /etc/ai mounts. Missing sources or a
        # healthy operator watcher are an execution failure, not a silent
        # unmonitored fallback. Older images retain their legacy mount layout.
        ai_enabled = (
            any((rootfs / item).exists() or (rootfs / item).is_symlink()
                for item in ("etc/ai", "etc/ai-past"))
            or self.runtime.ai_config_dir is not None
            or self.runtime.ai_history_dir is not None
            or self.ai_monitor is not None
            or self.operator_attachment is not None
        )
        config_ai: Path | None = None
        history_ai: Path | None = None
        if ai_enabled:
            for item in ("etc/ai", "etc/ai-past"):
                target = rootfs / item
                if not target.is_dir() or target.is_symlink():
                    raise CapabilityUnavailableError("system rootfs lacks safe /etc/ai mount targets")
            if (self.runtime.ai_config_dir is None or self.runtime.ai_history_dir is None
                    or (not isinstance(self.ai_monitor, AiConfigHistoryMonitor)
                        and self.operator_attachment is None)):
                raise CapabilityUnavailableError("AI configuration mounts require an active operator watcher")
            if self.runtime.ai_config_dir.is_symlink() or self.runtime.ai_history_dir.is_symlink():
                raise ScopeDeniedError("AI config mounts may not be symlinks")
            try:
                config_ai = self.runtime.ai_config_dir.resolve(strict=True)
                history_ai = self.runtime.ai_history_dir.resolve(strict=True)
            except OSError as exc:
                raise CapabilityUnavailableError("AI config/history sources are missing") from exc
            if not config_ai.is_dir() or not history_ai.is_dir():
                raise CapabilityUnavailableError("AI config/history sources must be directories")
            extended = (*sources, config_ai, history_ai)
            if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
                   for index, a in enumerate(extended) for b in extended[index + 1:]):
                raise ScopeDeniedError("AI config and history storage must be disjoint from sandbox assets")
            if not config_ai.stat().st_mode & stat.S_IWGRP:
                raise ScopeDeniedError("/etc/ai backing store must be workspace-group writable")
            if self.ai_monitor is not None:
                self.ai_monitor.require_active(config_dir=config_ai, past_dir=history_ai)
            elif self.operator_attachment is not None:
                # This is a live, mount-bound remote operator assertion; the
                # private socket itself is NEVER made visible inside the guest.
                self.operator_attachment.require_healthy()
        # Guest executable must be present inside immutable image; no host executable bind.
        if not argv or not argv[0].startswith("/"):
            raise InvalidInputError("guest command must use an absolute path")
        guest = argv[0]
        if guest.startswith("/usr/local/tools/"):
            # A shared, writable installation is NEVER sufficient authority
            # merely because an executable exists at this pathname. Its exact
            # bytes must be registered in the operator-owned runtime contract.
            import re
            if guest not in dict(self.runtime.tool_entrypoints):
                raise ScopeDeniedError("shared tool executable lacks operator admission")
            expected = dict(self.runtime.tool_entrypoints)[guest]
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise InvalidInputError("shared tool entrypoint hash is invalid")
            relative = guest.removeprefix("/usr/local/tools/")
            pieces = relative.split("/")
            if (len(pieces) < 3 or pieces[1] not in {"environment", "static"}
                    or any(piece in {"", ".", ".."} for piece in pieces)):
                raise ScopeDeniedError("tool entrypoint must be pinned reusable binary")
            candidate = tools
            for piece in pieces:
                candidate = candidate / piece
                if candidate.is_symlink():
                    raise ScopeDeniedError("tool entrypoint is redirected")
            guest_bin = candidate.resolve(strict=True)
            if not guest_bin.is_relative_to(tools) or not guest_bin.is_file():
                raise ScopeDeniedError("tool executable escapes the admitted tool root")
            if hashlib.sha256(guest_bin.read_bytes()).hexdigest() != expected:
                raise ScopeDeniedError("shared tool entrypoint differs from admission hash")
        else:
            guest_bin = (rootfs / guest.lstrip("/")).resolve(strict=True)
            if not guest_bin.is_relative_to(rootfs) or not guest_bin.is_file():
                raise ScopeDeniedError("guest executable is not part of the selected system image")
        guest_cwd = _guest_cwd(home, cwd)
        # A named agent's first directory becomes its shell HOME. This is a
        # working-directory convention only, not UID isolation.
        guest_parts = guest_cwd.split("/")
        guest_home = "/home" if len(guest_parts) < 3 else "/home/" + guest_parts[2]
        tool_write_source: Path | None = None
        if self.tool_write_grant is not None:
            # A writable installation mount is issued only by a trusted host
            # runner holding an active, exclusive per-tool maintenance lease.
            if guest.startswith("/usr/local/tools/"):
                raise ScopeDeniedError("installers must execute a verified rootfs program")
            tool_write_source = self.tool_write_grant.admit(
                runtime=self.runtime, cwd=cwd, guest_uid=guest_uid, guest_gid=guest_gid,
            )
        # --clearenv removes caller-supplied variables. Use the operator's
        # dedicated cache policy for installation, per-agent state for use.
        guest_env = model_environment()
        if tool_write_source is not None:
            from potbot.tools.shared_tool_store import tool_process_environment

            guest_env = tool_process_environment(
                self.tool_write_grant.slug, guest_home.split("/")[-1], installing=True
            )
        elif guest.startswith("/usr/local/tools/"):
            if guest_home == "/home":
                raise ScopeDeniedError("shared tools require an agent-local working directory")
            from potbot.tools.shared_tool_store import tool_process_environment

            guest_env = tool_process_environment(pieces[0], guest_home.split("/")[-1])
        seccomp = _seccomp_fd()
        try:
            args = [
                str(self.prlimit), f"--as={memory_bytes}", f"--cpu={cpu_seconds}",
                f"--fsize={max_file_bytes}", f"--nofile={max_open_files}", "--",
                str(self.executable), "--unshare-all", "--die-with-parent", "--new-session",
                "--cap-drop", "ALL", "--hostname", "potbot",
                # A requested synthetic UID must have a genuine user namespace.
                # --uid/--gid alone do not establish distinct *host* DAC IDs;
                # kernel-enforced cross-agent mode protection requires separate
                # idmapped filesystem/subuid qualification.
                *(("--unshare-user", "--uid", str(guest_uid),
                   "--gid", str(guest_gid)) if guest_uid is not None else ()),
                "--ro-bind", str(rootfs), "/", "--bind", str(home), "/home",
                "--bind", str(staging), "/var/ai",
                # Normal use can read, but never mutate a shared checkout.
                "--ro-bind", str(tools), "/usr/local/tools",
                *( ("--bind", str(tool_write_source),
                    "/usr/local/tools/" + self.tool_write_grant.slug)
                   if tool_write_source is not None else () ),
                "--ro-bind", str(library), "/opt/potbot/models",
                *(("--bind", str(config_ai), "/etc/ai",
                   "--ro-bind", str(history_ai), "/etc/ai-past") if ai_enabled else ()),
                "--proc", "/proc", "--dev", "/dev",
                "--clearenv", "--setenv", "HOME", guest_home,
                "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "TMPDIR", "/home/.tmp",
                "--setenv", "LANG", "C.UTF-8", "--setenv", "TERM", "xterm-256color",
                "--setenv", "SHELL", "/bin/bash",
                *(("--setenv", "POTBOT_AI_CONFIG", "/etc/ai",
                   "--setenv", "POTBOT_AI_HISTORY", "/etc/ai-past") if ai_enabled else ()),
                *[arg for key, value in guest_env.items()
                  for arg in ("--setenv", key, value)],
                "--chdir", guest_cwd,
                "--seccomp", str(seccomp), "--json-status-fd", str(status_fd), "--", *argv,
            ]
            identity = content_hash({
                "backend": "shared-rootfs-bwrap-seccomp-v2", "image": self.runtime.image_digest,
                "qualification": self.runtime.qualification_sha256,
                "rootfs": str(rootfs), "home": str(home), "cwd": guest_cwd,
                "network": "private-denied", "mounts": "rootfs-ro/home-rw/staging-rw/shared-tools-ro/models-ro/proc/dev",
                "model_staging": str(staging), "shared_models": str(library),
                "shared_tools": str(tools),
                "ai_mount_policy": "rw-config/ro-history/journaled-v1" if ai_enabled else "legacy-none",
                "ai_config": str(config_ai) if ai_enabled else None,
                "ai_history": str(history_ai) if ai_enabled else None,
                "writable_tool": self.tool_write_grant.slug if tool_write_source else None,
                "tool_write_lease": (
                    self.tool_write_grant.guard.lease.lease_id if tool_write_source else None
                ),
                "tool_entrypoints": sorted(self.runtime.tool_entrypoints),
                "guest_uid": guest_uid, "guest_gid": guest_gid,
                "kernel_agent_dac_qualified": False,
                "bwrap": hashlib.sha256(self.executable.read_bytes()).hexdigest(),
                "prlimit": hashlib.sha256(self.prlimit.read_bytes()).hexdigest(),
            })
            return IsolationLaunch(tuple(args), (seccomp, status_fd), identity)
        except BaseException:
            os.close(seccomp)
            raise
