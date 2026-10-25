"""Host-admitted Xpra application sessions in one qualified workspace rootfs.

No HTTP caller can choose argv, environment, mounts, image or process identity.
The operator reads a private policy, validates the active image pin, and uses
Bubblewrap with an explicit workspace X11 Unix-socket capability. Xpra supplies
window frames and input; this service owns only application process lifetimes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import content_hash
from potbot.tools.isolation import IsolationLaunch, _seccomp_fd

_KIND = re.compile(r"[a-z][a-z0-9-]{1,31}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class AppRecipe:
    kind: str
    title: str
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _KIND.fullmatch(self.kind) or not (1 <= len(self.title) <= 80):
            raise InvalidInputError("invalid operator application identity")
        if (not 1 <= len(self.argv) <= 24 or not self.argv[0].startswith("/") or
                any(not isinstance(x, str) or not x or len(x) > 256 or "\x00" in x
                    for x in self.argv)):
            raise InvalidInputError("applications require bounded, pinned guest argv")
        if self.argv[0] in {"/bin/sh", "/bin/bash", "/usr/bin/env"}:
            raise ScopeDeniedError("GUI recipes must launch an explicit graphical application")


@dataclass(frozen=True)
class AppView:
    id: str
    kind: str
    title: str
    state: str
    detached: bool
    started_at: int
    exit_code: int | None

    def document(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "title": self.title,
                "state": self.state, "detached": self.detached,
                "startedAt": self.started_at, "exitCode": self.exit_code}


class XpraSandboxLaunchBackend:
    """Wrap an already-qualified shared-rootfs backend with ONE live Xpra grant.

    The normal sandbox IPC profile is not weakened: only this operator-created
    wrapper swaps to AF_UNIX-scoped seccomp. Its physical X11 socket is the
    current workspace's owned socket, and the guest network remains unshared.
    """

    def __init__(self, *, server: Any, backend: Any, ledger: Any) -> None:
        from potbot.tools.shared_workspace_isolation import SharedWorkspaceBubblewrapBackend
        if not isinstance(backend, SharedWorkspaceBubblewrapBackend):
            raise CapabilityUnavailableError("Xpra applications require qualified shared-rootfs Bubblewrap")
        self.server, self.backend, self.ledger = server, backend, ledger
        self.runtime = backend.runtime
        if self.runtime.home.resolve(strict=True) != server.sandbox_root or server.workspace_id is None:
            raise ScopeDeniedError("Xpra display does not belong to the pinned workspace")

    def prepare(self, *, argv: tuple[str, ...], status_fd: int) -> IsolationLaunch:
        from potbot.tools.workspace_supervision import validate_runtime_pin
        from potbot.tools.shared_workspace_isolation import SharedWorkspaceBubblewrapBackend
        assert isinstance(self.backend, SharedWorkspaceBubblewrapBackend)
        validate_runtime_pin(self.ledger, self.runtime, self.server.workspace_id)
        # Query the running display on EVERY admission, never trust a stale
        # socket file, a browser-supplied display number or caller's DISPLAY.
        env = self.server.environment()
        socket_path = self.server.socket_path
        auth = self.server.auth_file
        home = self.runtime.home.resolve(strict=True)
        if (not socket_path.is_socket() or socket_path.is_symlink() or
                not auth.is_file() or auth.is_symlink() or auth.resolve().parent != home / ".potbot" / "x11" or
                auth.stat().st_uid != os.getuid() or auth.stat().st_mode & 0o077):
            raise ScopeDeniedError("Xpra socket or Xauthority lost its operator-owned identity")
        # /tmp is a rootfs symlink to /home/.tmp. Mount a single X socket at
        # /home/.tmp/.X11-unix/XNNN (never host /tmp or the socket directory).
        guest_socket_dir = home / ".tmp" / ".X11-unix"
        if guest_socket_dir.is_symlink():
            raise ScopeDeniedError("Xpra socket destination directory is redirected")
        guest_socket_dir.mkdir(mode=0o700, exist_ok=True)
        if not guest_socket_dir.is_dir() or guest_socket_dir.stat().st_uid != os.getuid():
            raise ScopeDeniedError("Xpra guest socket directory is not operator-owned")
        target = guest_socket_dir / socket_path.name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ScopeDeniedError("Xpra socket target was redirected")
        fd = os.open(target, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        kwargs = dict(argv=argv, workspace_root=home, cwd=home,
                      read_only_roots=(), writable_roots=(), sandbox_root=home,
                      status_fd=status_fd, memory_bytes=2 * 1024**3,
                      cpu_seconds=3600, max_file_bytes=128 * 1024**2,
                      max_open_files=256)
        launch = self.backend.prepare(**kwargs)
        replacement = -1
        try:
            replacement = _seccomp_fd(private_x11_unix=True)
            args = list(launch.argv)
            if (args.count("--seccomp") != 1 or args.count("--proc") != 1 or
                    len(launch.pass_fds) != 2 or launch.pass_fds[0] == status_fd):
                raise CapabilityUnavailableError("sandbox launch structure does not support Xpra IPC")
            old_fd = launch.pass_fds[0]
            sec_index = args.index("--seccomp")
            if args[sec_index + 1] != str(old_fd) or args.count("--unshare-all") != 1:
                raise CapabilityUnavailableError("sandbox network or seccomp policy changed")
            args[sec_index + 1] = str(replacement)
            mount = args.index("--proc")
            args[mount:mount] = ["--ro-bind", str(socket_path),
                                  "/home/.tmp/.X11-unix/" + socket_path.name]
            chdir = args.index("--chdir")
            args[chdir:chdir] = ["--setenv", "DISPLAY", env["DISPLAY"],
                                 "--setenv", "XAUTHORITY", "/home/.potbot/x11/Xauthority"]
            identity = content_hash({"base": launch.identity, "transport": "xpra-unix-v1",
                                     "workspace": str(self.server.workspace_id),
                                     "socket": str(socket_path),
                                     "socket_inode": socket_path.stat().st_ino,
                                     "authority_digest": hashlib.sha256(auth.read_bytes()).hexdigest()})
            os.close(old_fd)
            return IsolationLaunch(tuple(args), (replacement, status_fd), identity)
        except BaseException:
            if replacement >= 0:
                os.close(replacement)
            for original in launch.pass_fds:
                if original != status_fd:
                    os.close(original)
            raise


class _App:
    def __init__(self, identifier: str, recipe: AppRecipe,
                 process: subprocess.Popen[bytes], identity: str) -> None:
        self.id, self.recipe, self.process, self.identity = identifier, recipe, process, identity
        self.started_at = int(time.time())
        self.detached = False
        self.indeterminate = False
        self.browser_control: Any = None

    def view(self) -> AppView:
        exit_code = self.process.poll()
        return AppView(self.id, self.recipe.kind, self.recipe.title,
                       "indeterminate" if self.indeterminate else "running" if exit_code is None else "exited",
                       self.detached, self.started_at, exit_code)


class XpraApplicationSupervisor:
    """Operator-owned sessions; closing a Dockview pane NEVER stops a process.

    One workspace-wide lease covers all concurrently running GUI applications.
    The exclusive lease is released only when every process has actually ended.
    This conservatively fences overlapping unbrokered writes to the shared home.
    """

    def __init__(self, *, workspace_id: UUID, principal_id: UUID, recipes: tuple[AppRecipe, ...],
                 backend: XpraSandboxLaunchBackend, ledger: Any,
                 popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
                 ai_monitor: Any = None,
                 max_apps: int = 4, max_age_seconds: int = 3600) -> None:
        if (not isinstance(workspace_id, UUID) or not isinstance(principal_id, UUID) or
                workspace_id != backend.server.workspace_id or not 1 <= max_apps <= 12):
            raise ScopeDeniedError("app supervisor must have one admitted workspace and principal")
        if not recipes or len(recipes) > 12 or len({r.kind for r in recipes}) != len(recipes):
            raise InvalidInputError("app recipes must be a bounded operator allowlist")
        self.workspace_id, self.principal_id = workspace_id, principal_id
        self.recipes = {r.kind: r for r in recipes}
        self.backend, self.ledger, self.popen = backend, ledger, popen
        self.ai_monitor = ai_monitor
        self.max_apps, self.max_age_seconds = max_apps, max_age_seconds
        self._lock = threading.RLock()
        self._apps: dict[str, _App] = {}
        self._group_ids: set[str] = set()
        self._guard: Any = None
        self._stop = threading.Event()
        self._watcher = threading.Thread(target=self._watch, name="potbot-xpra-app-watch", daemon=True)
        self._watcher.start()

    def _settle(self) -> None:
        """Called under lock. Never release lease for an unconfirmed process tree."""
        if self._guard is None or any(
            self._apps[identifier].process.poll() is None for identifier in self._group_ids
        ):
            return
        group = tuple(self._group_ids)
        self._group_ids.clear()
        guard, self._guard = self._guard, None
        try:
            from potbot.tools.workspace_supervision import admission_digest
            guard.finish(evidence_sha256=admission_digest("xpra-apps-all-stopped"),
                         uncertain=guard.fenced or any(self._apps[identifier].indeterminate for identifier in group))
        except Exception:
            guard.abandon()

    def _terminate(self, app: _App) -> None:
        proc = app.process
        if proc.poll() is not None:
            if app.browser_control is not None:
                app.browser_control.close()
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                app.indeterminate = True
        if app.browser_control is not None:
            app.browser_control.close()

    def _watch(self) -> None:
        while not self._stop.wait(0.25):
            with self._lock:
                guard = self._guard
                if guard is None:
                    continue
                expired = any(app.process.poll() is None and
                              time.time() - app.started_at > self.max_age_seconds
                              for app in self._apps.values())
                try:
                    self.backend.server.environment()  # crash or lost X socket => fence GUI apps
                    lost_display = False
                except (OSError, RuntimeError):
                    lost_display = True
                lost_watcher = self.ai_monitor is not None and not self.ai_monitor.is_healthy()
                if guard.fenced or expired or lost_display or lost_watcher:
                    for app in self._apps.values():
                        self._terminate(app)
                        if guard.fenced or lost_display or lost_watcher:
                            app.indeterminate = True
                self._settle()

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            self._settle()
            return [app.view().document() for app in sorted(self._apps.values(), key=lambda a: a.started_at)] [-100:]

    def choices(self) -> list[dict[str, str]]:
        return [{"kind": r.kind, "title": r.title} for r in self.recipes.values()]

    def start(self, kind: str) -> dict[str, Any]:
        if not isinstance(kind, str) or kind not in self.recipes:
            raise ScopeDeniedError("application kind is not on the operator allowlist")
        with self._lock:
            self._settle()
            if sum(a.process.poll() is None for a in self._apps.values()) >= self.max_apps:
                raise StateMismatchError("workspace application capacity reached")
            first = self._guard is None
            if first:
                from potbot.tools.workspace_supervision import WorkspaceLeaseGuard, WorkspaceExecutorIdentity
                runtime = self.backend.runtime
                guard = WorkspaceLeaseGuard.exclusive_home(
                    self.ledger, WorkspaceExecutorIdentity(self.workspace_id, self.principal_id,
                                                           runtime.image_digest),
                    operation="xpra.apps", idempotency_key="xpra-" + uuid4().hex)
                guard.start()
                self._guard = guard
            if self._guard.fenced:
                raise StateMismatchError("workspace application lease has been fenced")
            write_fd = os.open(os.devnull, os.O_WRONLY)
            launch = None
            process = None
            try:
                launch = self.backend.prepare(argv=self.recipes[kind].argv, status_fd=write_fd)
                controlled_browser = kind == "controlled-browser"
                process = self.popen(launch.argv,
                                     stdin=subprocess.PIPE if controlled_browser else subprocess.DEVNULL,
                                     stdout=subprocess.PIPE if controlled_browser else subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     cwd="/", env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                                     close_fds=True, pass_fds=launch.pass_fds,
                                     start_new_session=True)
                os.close(write_fd); write_fd = -1
                # Bwrap writes its small JSON status to /dev/null, not a pipe
                # whose undrained buffer could stall a long-lived process.
                if process.poll() is not None:
                    raise CapabilityUnavailableError("sandbox application exited during admission")
                app = _App(uuid4().hex, self.recipes[kind], process, launch.identity)
                if controlled_browser:
                    from potbot.tools.xpra_browser_control import BrowserControlChannel
                    app.browser_control = BrowserControlChannel(process)
                self._apps[app.id] = app
                self._group_ids.add(app.id)
                # Bounded operator state; live applications are never pruned.
                if len(self._apps) > 200:
                    retired = [key for key, old in self._apps.items()
                               if key not in self._group_ids and old.process.poll() is not None]
                    for key in retired[:len(self._apps)-200]:
                        del self._apps[key]
                return app.view().document()
            except BaseException:
                if process is not None:
                    # A child that exited during admission may already have
                    # mutated the home. Preserve an uncertain receipt even if
                    # it is now stopped; never call this successful work.
                    temporary = _App(uuid4().hex, self.recipes[kind], process, "")
                    self._terminate(temporary)
                    temporary.indeterminate = True
                    self._apps[temporary.id] = temporary
                    self._group_ids.add(temporary.id)
                self._settle()
                raise
            finally:
                for fd in (write_fd, *(launch.pass_fds if launch is not None else ())):
                    if fd >= 0:
                        try: os.close(fd)
                        except OSError: pass

    def browser_command(self, identifier: str, command: object) -> dict[str, Any]:
        """Read/control only the Chromium belonging to an admitted app session."""
        with self._lock:
            app = self._get(identifier)
            if (app.recipe.kind != "controlled-browser" or app.process.poll() is not None
                    or app.indeterminate or self._guard is None or self._guard.fenced
                    or app.browser_control is None):
                raise ScopeDeniedError("browser operation outside admitted live session")
            channel = app.browser_control
        try:
            return channel.call(command)
        except StateMismatchError:
            with self._lock:
                app.indeterminate = True
                self._terminate(app)
                self._settle()
            raise

    def detach(self, identifier: str) -> dict[str, Any]:
        with self._lock:
            app = self._get(identifier)
            app.detached = True
            return app.view().document()

    def attach(self, identifier: str) -> dict[str, Any]:
        with self._lock:
            app = self._get(identifier)
            app.detached = False
            return app.view().document()

    def terminate(self, identifier: str) -> dict[str, Any]:
        with self._lock:
            app = self._get(identifier)
            self._terminate(app)
            self._settle()
            return app.view().document()

    def _get(self, identifier: str) -> _App:
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or identifier not in self._apps:
            raise ScopeDeniedError("application does not belong to this workspace")
        return self._apps[identifier]

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            for app in self._apps.values():
                self._terminate(app)
            self._settle()
        self._watcher.join(timeout=2)
        if self.ai_monitor is not None:
            self.ai_monitor.stop()


def load_operator_application_policy(path: Path, server: Any) -> XpraApplicationSupervisor:
    """Load PRIVATE host-only policy with an independently pinned runtime.

    File must belong to the bridge operator and be outside workspace home and
    immutable rootfs. It is configuration, NOT an attestation; the image must
    already be qualified and pinned in the controller-owned lease ledger.
    """
    from potbot.tools.shared_workspace_isolation import SharedWorkspaceRuntime, SharedWorkspaceBubblewrapBackend
    from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger
    from potbot.tools.workspace_supervision import validate_runtime_pin
    if (path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid() or
            stat.S_IMODE(path.stat().st_mode) & 0o077):
        raise ScopeDeniedError("Xpra application policy must be private and operator-owned")
    if path.stat().st_size > 8192:
        raise InvalidInputError("Xpra application policy is too large")
    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate application policy key")
            result[key] = value
        return result
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)
        if not isinstance(data, dict) or set(data) != {"workspaceId", "principalId", "runtime", "ledger", "recipes"}:
            raise ValueError()
        if UUID(data["workspaceId"]) != server.workspace_id:
            raise ScopeDeniedError("application policy belongs to another workspace")
        principal = UUID(data["principalId"])
        obj = data["runtime"]
        required = {"imageDigest", "qualificationSha256", "rootfs", "home", "modelStaging", "sharedModels", "sharedTools"}
        ai_keys = {"aiConfig", "aiPast"}
        if not isinstance(obj, dict) or (set(obj) not in (required, required | ai_keys)):
            raise ValueError()
        paths = {k: Path(obj[k]).resolve(strict=True) for k in ("rootfs", "home", "modelStaging", "sharedModels", "sharedTools")}
        if path.resolve().is_relative_to(paths["home"]) or path.resolve().is_relative_to(paths["rootfs"]):
            raise ScopeDeniedError("Xpra policy must be outside guest filesystems")
        ai_config = Path(obj["aiConfig"]).resolve(strict=True) if "aiConfig" in obj else None
        ai_past = Path(obj["aiPast"]).resolve(strict=True) if "aiPast" in obj else None
        runtime = SharedWorkspaceRuntime(
            image_digest=obj["imageDigest"], qualification_sha256=obj["qualificationSha256"],
            rootfs=paths["rootfs"], home=paths["home"], model_staging=paths["modelStaging"],
            shared_models=paths["sharedModels"], shared_tools=paths["sharedTools"],
            ai_config_dir=ai_config, ai_history_dir=ai_past,
        )
        ledger = WorkspaceLeaseLedger(Path(data["ledger"]), workspace_home=paths["home"])
        validate_runtime_pin(ledger, runtime, server.workspace_id)
        raw = data["recipes"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 12:
            raise ValueError()
        recipes = tuple(AppRecipe(row["kind"], row["title"], tuple(row["argv"])) for row in raw)
    except (ValueError, TypeError, KeyError, OSError, json.JSONDecodeError) as exc:
        raise InvalidInputError("invalid operator application policy") from exc
    # Modern rootfs images require the same active, integrity-checked /etc/ai
    # monitor as other workspace executors. If another operator owns its
    # singleton watcher, this second operator cannot launch processes.
    monitor = None
    if ai_config is not None:
        from potbot.tools.ai_config_history import AiConfigHistoryMonitor
        monitor = AiConfigHistoryMonitor(config_dir=ai_config, past_dir=ai_past)
        monitor.start()
    try:
        backend = SharedWorkspaceBubblewrapBackend(runtime, ai_monitor=monitor)
        xbackend = XpraSandboxLaunchBackend(server=server, backend=backend, ledger=ledger)
        return XpraApplicationSupervisor(workspace_id=server.workspace_id, principal_id=principal,
                                         recipes=recipes, backend=xbackend, ledger=ledger,
                                         ai_monitor=monitor)
    except BaseException:
        if monitor is not None:
            monitor.stop()
        raise
