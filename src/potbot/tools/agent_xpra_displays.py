"""S40: independent operator-owned Xpra display and mount boundary per agent.

The shared X11 display is *not* an isolation boundary.  Each agent browser
is instead launched in its own Xpra/Xdummy display, with an Xauthority cookie
held outside the workspace's shared /home.  The sandbox receives only its
own agent home, a temporary X11 socket bind, and a sealed read-only cookie.

This is host code: never allow a model, browser HTTP request or untrusted
metadata to construct an AgentDisplayPool or supply its backing paths.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from potbot.domain.errors import (CapabilityUnavailableError, InvalidInputError,
                                  ScopeDeniedError, StateMismatchError)
from potbot.domain.serialization import content_hash
from potbot.tools.isolation import IsolationLaunch, _seccomp_fd

# The private display is deliberately view-only.  Browser input goes through
# the already reserved Playwright/CDP RPC, not through native Xpra input.
_COOKIE_GUEST = "/home/.tmp/potbot-agent-Xauthority"
_MAX_DISPLAYS = 12


def _private_directory(path: Path, *, must_exist: bool = True) -> Path:
    """Validate every ancestor below the explicitly configured operator root."""
    if not path.is_absolute() or path.is_symlink():
        raise ScopeDeniedError("Xpra private display requires a real absolute host directory")
    if must_exist and not path.is_dir():
        raise CapabilityUnavailableError("private Xpra display directory is not provisioned")
    if path.exists():
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or (
            stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ScopeDeniedError("private Xpra display directory has unsafe ownership or permissions")
    return path.resolve(strict=must_exist)


@dataclass(frozen=True, slots=True)
class AgentXpraDisplay:
    agent_id: UUID
    home_name: str
    server: Any

    @property
    def display(self) -> str:
        return self.server.display


class AgentXpraDisplayPool:
    """Operator-private server per admitted agent, no global display sharing.

    A registered agent must have a **different** display from every other
    agent.  A display is not recycled while a process may still be using it.
    Startup errors fail closed and are not interpreted as an empty display.
    """

    def __init__(self, *, workspace_id: UUID, principal_id: UUID, operator_root: Path,
                 runtime: Any, ledger: Any, homes: Any, backend: Any,
                 server_factory: Callable[..., Any] | None = None,
                 browser_gateway: Any = None, maximum: int = _MAX_DISPLAYS) -> None:
        from potbot.tools.agent_homes import AgentHomeRegistry
        from potbot.tools.shared_workspace_isolation import (SharedWorkspaceBubblewrapBackend,
                                                             SharedWorkspaceRuntime)
        if (not isinstance(workspace_id, UUID) or not isinstance(principal_id, UUID)
                or not isinstance(runtime, SharedWorkspaceRuntime)
                or not isinstance(backend, SharedWorkspaceBubblewrapBackend)
                or backend.runtime != runtime or not isinstance(homes, AgentHomeRegistry)
                or homes.ledger is not ledger or not 1 <= maximum <= _MAX_DISPLAYS):
            raise ScopeDeniedError("agent display pool needs the qualified workspace host")
        root = _private_directory(Path(operator_root))
        for other in (runtime.home, runtime.rootfs, runtime.model_staging,
                      runtime.shared_tools, runtime.shared_models,
                      runtime.ai_config_dir, runtime.ai_history_dir):
            if other is None:
                continue
            resolved = Path(other).resolve(strict=True)
            if root == resolved or root.is_relative_to(resolved) or resolved.is_relative_to(root):
                raise ScopeDeniedError("private Xpra display root overlaps guest or shared storage")
        if server_factory is None:
            from potbot.tools.x11_workspace_server import WorkspaceXServer
            server_factory = WorkspaceXServer
        self.workspace_id, self.principal_id = workspace_id, principal_id
        self.runtime, self.ledger, self.homes, self.backend = runtime, ledger, homes, backend
        if browser_gateway is not None:
            from potbot.tools.browser_connect_gateway import BrowserConnectGateway
            if not isinstance(browser_gateway, BrowserConnectGateway):
                raise ScopeDeniedError("agent display egress requires the operator-owned broker")
        self.browser_gateway = browser_gateway
        self.root, self.maximum, self.server_factory = root, maximum, server_factory
        self._lock = threading.RLock()
        self._displays: dict[UUID, AgentXpraDisplay] = {}

    def _agent(self, agent_id: UUID) -> Any:
        from potbot.tools.workspace_supervision import validate_runtime_pin
        if not isinstance(agent_id, UUID):
            raise InvalidInputError("agent display identity is not a UUID")
        validate_runtime_pin(self.ledger, self.runtime, self.workspace_id)
        home = self.homes.lookup(self.workspace_id, agent_id)
        if home is None or home.agent_id != agent_id or home.workspace_id != self.workspace_id:
            raise ScopeDeniedError("Xpra browser agent is not enrolled in this workspace")
        if (not home.directory or "/" in home.directory or home.directory in {".", "..", ".tmp"}):
            raise ScopeDeniedError("agent home has an unsafe namespace")
        path = self.runtime.home / home.directory
        if path.is_symlink() or not path.is_dir() or path.resolve(strict=True).parent != self.runtime.home.resolve(strict=True):
            raise ScopeDeniedError("agent home is not a real registered directory")
        return home

    def acquire(self, agent_id: UUID) -> AgentXpraDisplay:
        home = self._agent(agent_id)
        with self._lock:
            existing = self._displays.get(agent_id)
            if existing is not None:
                if existing.home_name != home.directory:
                    raise StateMismatchError("agent home changed during an active GUI display")
                existing.server.environment()  # must be genuinely live
                return existing
            if len(self._displays) >= self.maximum:
                raise StateMismatchError("agent display capacity reached")
            _private_directory(self.root)
            folder = self.root / agent_id.hex
            if folder.is_symlink():
                raise ScopeDeniedError("agent display was redirected")
            folder.mkdir(mode=0o700, exist_ok=True)
            _private_directory(folder)
            # Display root is operator-owned, **not** under /home. Therefore a
            # different agent cannot read even the Xauthority cookie pathname.
            server = self.server_factory(self.workspace_id, folder, readonly_mode=True)
            try:
                server.start()
                server.environment()
            except BaseException:
                server.stop()
                raise
            selected = AgentXpraDisplay(agent_id, home.directory, server)
            self._displays[agent_id] = selected
            return selected

    def environment(self, agent_id: UUID) -> dict[str, str]:
        with self._lock:
            display = self._displays.get(agent_id)
            if display is None:
                raise CapabilityUnavailableError("agent Xpra display was not started")
            return display.server.environment()

    def retire(self, agent_id: UUID, *, process_stopped: bool) -> None:
        if process_stopped is not True:
            raise StateMismatchError("cannot retire agent display with unconfirmed processes")
        with self._lock:
            display = self._displays.pop(agent_id, None)
            if display is not None:
                display.server.stop()

    def stop_all(self) -> None:
        """Only operator shutdown after the application supervisor stops workers."""
        with self._lock:
            for display in self._displays.values():
                display.server.stop()
            self._displays.clear()

    def prepare(self, agent_id: UUID, *, argv: tuple[str, ...], status_fd: int,
                memory_bytes: int = 2 * 1024**3, cpu_seconds: int = 3600,
                max_file_bytes: int = 128 * 1024**2,
                max_open_files: int = 256) -> IsolationLaunch:
        """Construct bwrap argv with no access to the shared /home or X cookie.

        One X server is issued per agent, and only a dedicated agent browser
        process should use this method.  The main sandbox continues to deny
        network access; this wrapper changes only the X11 AF_UNIX capability.
        """
        if (type(memory_bytes) is not int or not 16_777_216 <= memory_bytes <= 2 * 1024**3
                or type(cpu_seconds) is not int or not 1 <= cpu_seconds <= 3600
                or type(max_file_bytes) is not int or not 1 <= max_file_bytes <= 128 * 1024**2
                or type(max_open_files) is not int or not 32 <= max_open_files <= 256):
            raise ScopeDeniedError("agent browser limits must not exceed the approved envelope")
        display = self.acquire(agent_id)
        home = self._agent(agent_id)
        info = display.server.environment()
        x_socket = display.server.socket_path
        authority = display.server.auth_file
        if (not x_socket.is_socket() or x_socket.is_symlink()
                or authority.is_symlink() or not authority.is_file()
                or authority.stat().st_uid != os.getuid()
                or authority.stat().st_mode & 0o077
                or not authority.resolve(strict=True).is_relative_to(self.root)):
            raise ScopeDeniedError("agent X11 socket or authority identity changed")
        # Normal shared workspace bwrap admission (image pin, hash of executable,
        # AI config watcher and tools), before narrowing this process's mounts.
        launch = self.backend.prepare(
            argv=argv, workspace_root=self.runtime.home, sandbox_root=self.runtime.home,
            cwd=self.runtime.home / home.directory, read_only_roots=(), writable_roots=(),
            guest_uid=home.uid, guest_gid=home.gid, status_fd=status_fd,
            memory_bytes=memory_bytes, cpu_seconds=cpu_seconds,
            max_file_bytes=max_file_bytes, max_open_files=max_open_files,
        )
        seccomp_fd = -1
        cookie_fd = -1
        broker_fd = -1
        try:
            args = list(launch.argv)
            old_fd = launch.pass_fds[0]
            # Reject unknown bwrap structures before modifying a mount list.
            if (len(launch.pass_fds) != 2 or launch.pass_fds[1] != status_fd
                    or args.count("--seccomp") != 1 or args.count("--unshare-all") != 1
                    or args.count("--clearenv") != 1 or args.count("--chdir") != 1
                    or args.count("--proc") != 1):
                raise CapabilityUnavailableError("qualified agent browser mount structure changed")
            index = args.index("--seccomp")
            if args[index + 1] != str(old_fd):
                raise ScopeDeniedError("sandbox seccomp identity does not match its descriptor")
            host_home = self.runtime.home.resolve(strict=True)
            guest_home = f"/home/{home.directory}"
            mount_key = ["--bind", str(host_home), "/home"]
            candidates = [n for n in range(len(args)-2) if args[n:n+3] == mount_key]
            if len(candidates) != 1:
                raise CapabilityUnavailableError("agent browser requires one exact shared-home mount")
            pos = candidates[0]
            # Hide **all** other agents' home trees and the workspace-level X
            # authority. /home/.tmp becomes a private per-browser tmpfs.
            isolated_mount = [
                "--tmpfs", "/home",
                "--dir", "/home/.tmp",
                "--dir", "/home/.tmp/.X11-unix",
                "--dir", guest_home,
                "--bind", str(host_home / home.directory), guest_home,
                "--ro-bind", str(x_socket), "/home/.tmp/.X11-unix/" + x_socket.name,
            ]
            args[pos:pos + 3] = isolated_mount
            # Model downloads and their caches are not a browser capability.
            # Mask shared staging, as a web page must not read another agent's
            # model cache or installation scratch assets.
            if self.runtime.model_staging is not None:
                staging = ["--bind", str(self.runtime.model_staging.resolve(strict=True)), "/var/ai"]
                matches = [n for n in range(len(args)-2) if args[n:n+3] == staging]
                if len(matches) != 1:
                    raise CapabilityUnavailableError("agent browser staging mount differs from qualification")
                args[matches[0]:matches[0] + 3] = ["--tmpfs", "/var/ai"]
            # Existing AI configuration is operator-monitored but need not be
            # mutable by browser content. Downgrade its mount to read-only.
            if self.runtime.ai_config_dir is not None:
                config = ["--bind", str(self.runtime.ai_config_dir.resolve(strict=True)), "/etc/ai"]
                matches = [n for n in range(len(args)-2) if args[n:n+3] == config]
                if len(matches) != 1:
                    raise CapabilityUnavailableError("agent browser AI mount differs from qualification")
                args[matches[0]] = "--ro-bind"
            if self.browser_gateway is not None:
                # Broker ingress is UNIX only; only this agent's mount namespace
                # gains one broker socket. The normal sandbox network remains
                # private/denied. Tokens are FD-bound, not argv/env values.
                gateway = self.browser_gateway
                source = gateway.path
                meta = source.lstat()
                if (source.is_symlink() or not stat.S_ISSOCK(meta.st_mode)
                        or meta.st_uid != os.getuid()
                        or stat.S_IMODE(meta.st_mode) != 0o600):
                    raise ScopeDeniedError("agent browser broker socket is unavailable")
                if not hasattr(os, "memfd_create"):
                    raise CapabilityUnavailableError("broker token FD unavailable")
                broker_fd = os.memfd_create("potbot-agent-browser-egress", os.MFD_CLOEXEC)
                os.write(broker_fd, gateway.token.encode("ascii"))
                os.lseek(broker_fd, 0, os.SEEK_SET)
                insert = args.index("--proc")
                args[insert:insert] = [
                    "--dir", "/home/.tmp/.potbot-connect",
                    "--ro-bind", str(source), "/home/.tmp/.potbot-connect/egress.sock",
                    "--ro-bind-data", str(broker_fd), "/home/.tmp/.potbot-connect/token",
                ]
                env_at = args.index("--chdir")
                args[env_at:env_at] = [
                    "--setenv", "POTBOT_BROWSER_CONNECT_SOCKET", "/home/.tmp/.potbot-connect/egress.sock",
                    "--setenv", "POTBOT_BROWSER_CONNECT_TOKEN_FILE", "/home/.tmp/.potbot-connect/token",
                ]
            # Enforce anonymous sealed data, not a readable credential file in
            # /home and not a secret stored in an environment/argv string.
            if not hasattr(os, "memfd_create") or not hasattr(os, "MFD_ALLOW_SEALING"):
                raise CapabilityUnavailableError("sealed memfd Xauthority capability unavailable")
            cookie_fd = os.memfd_create("potbot-agent-xauth", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            content = authority.read_bytes()
            if not 1 <= len(content) <= 4096:
                raise ScopeDeniedError("agent Xauthority capability has an invalid byte size")
            os.write(cookie_fd, content)
            fcntl.fcntl(cookie_fd, fcntl.F_ADD_SEALS,
                        fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE)
            os.lseek(cookie_fd, 0, os.SEEK_SET)
            args[args.index("--proc"):args.index("--proc")] = [
                "--ro-bind-data", str(cookie_fd), _COOKIE_GUEST,
            ]
            # Keep X11 socket restricted to this display's separate mount
            # namespace. Synthetic guest UIDs are NOT evidence of host DAC.
            cursor = args.index("--chdir")
            args[cursor:cursor] = [
                "--setenv", "DISPLAY", info["DISPLAY"],
                "--setenv", "XAUTHORITY", _COOKIE_GUEST,
                "--setenv", "XDG_CACHE_HOME", guest_home + "/.cache",
            ]
            seccomp_fd = _seccomp_fd(private_x11_unix=True)
            args[args.index("--seccomp") + 1] = str(seccomp_fd)
            identity = content_hash({
                "base": launch.identity, "profile": "agent-xpra-private-namespace-v1",
                "agent": str(agent_id), "workspace": str(self.workspace_id),
                "home": home.directory, "server_display": display.display,
                "socket_inode": x_socket.stat().st_ino,
                "cookie_sha256": hashlib.sha256(content).hexdigest(),
                "shared_home_hidden": True, "model_staging_hidden": True,
                "broker_inode": (self.browser_gateway.path.stat().st_ino
                    if self.browser_gateway is not None else None),
            })
            os.close(old_fd)
            return IsolationLaunch(tuple(args), (seccomp_fd, cookie_fd, status_fd) +
                                   ((broker_fd,) if broker_fd >= 0 else ()), identity)
        except BaseException:
            for fd in (seccomp_fd, cookie_fd, broker_fd, launch.pass_fds[0]):
                if fd >= 0:
                    try: os.close(fd)
                    except OSError: pass
            raise


from potbot.tools.shared_workspace_isolation import SharedWorkspaceBubblewrapBackend


class AgentXpraSandboxBackend(SharedWorkspaceBubblewrapBackend):
    """ProcessSupervisor-compatible adapter for *one registered* agent display.

    The supervisor still performs its ordinary agent telemetry, syscall/FD
    monitoring and independent result auditing. The adapter has no unqualified
    launch fallback, arbitrary mounts or model-supplied display selection.
    """

    guest_executable = True

    def __init__(self, pool: AgentXpraDisplayPool, agent_id: UUID) -> None:
        if not isinstance(pool, AgentXpraDisplayPool) or not isinstance(agent_id, UUID):
            raise ScopeDeniedError("isolated Xpra supervisor needs a host-created agent pool")
        home = pool._agent(agent_id)
        self.pool = pool
        self.agent_id = agent_id
        self.runtime = pool.runtime
        self._home = home
        self.operator_attachment = getattr(pool.backend, "operator_attachment", None)

    def prepare(self, *, argv: tuple[str, ...], workspace_root: Path, cwd: Path,
                read_only_roots: tuple[Path, ...], writable_roots: tuple[Path, ...],
                sandbox_root: Path, status_fd: int, memory_bytes: int, cpu_seconds: int,
                max_file_bytes: int, max_open_files: int,
                ipc_profile: str = "none", guest_uid: int | None = None,
                guest_gid: int | None = None) -> IsolationLaunch:
        home = self.pool._agent(self.agent_id)
        if (read_only_roots or writable_roots or ipc_profile != "none"
                or workspace_root.resolve(strict=True) != self.runtime.home.resolve(strict=True)
                or sandbox_root.resolve(strict=True) != self.runtime.home.resolve(strict=True)
                or cwd.resolve(strict=True) != (self.runtime.home / home.directory).resolve(strict=True)
                or guest_uid != home.uid or guest_gid != home.gid
                or not 1 <= memory_bytes <= 2 * 1024**3
                or not 1 <= cpu_seconds <= 3600
                or not 1 <= max_file_bytes <= 128 * 1024**2
                or not 1 <= max_open_files <= 256):
            raise ScopeDeniedError("agent Xpra supervision differs from its pinned sandbox limits")
        return self.pool.prepare(self.agent_id, argv=argv, status_fd=status_fd,
                                 memory_bytes=memory_bytes, cpu_seconds=cpu_seconds,
                                 max_file_bytes=max_file_bytes, max_open_files=max_open_files)
