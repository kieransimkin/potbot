"""Host-controlled workspace Bash PTYs over the qualified shared-rootfs sandbox.

A pane detach never stops its shell. Sessions are owned by this host process:
restarting the controller intentionally leaves an expired, indeterminate lease
until an operator establishes that the orphaned process was terminated. This is
not yet a cluster-wide PTY migration or a browser-facing WebSocket service.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import select
import signal
import struct
import subprocess
import termios
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.tools.agent_homes import AgentHomeRegistry
from potbot.tools.shared_workspace_isolation import (
    SharedWorkspaceBubblewrapBackend,
    SharedWorkspaceRuntime,
)
from potbot.tools.supervisor import ProcessRequest
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger
from potbot.tools.workspace_supervision import (
    WorkspaceExecutorIdentity,
    WorkspaceLeaseGuard,
    admission_digest,
    validate_runtime_pin,
)


@dataclass(frozen=True, slots=True)
class BashSessionView:
    id: str
    workspace_id: UUID
    actor_id: UUID
    image_digest: str
    isolation_identity: str
    running: bool
    detached: bool
    exit_code: int | None
    started_ns: int
    guest_home: str = "/home"
    simulated_uid: int | None = None
    simulated_gid: int | None = None
    kernel_identity_qualified: bool = False


class _BashSession:
    def __init__(
        self, *, session_id: str, workspace_id: UUID, actor_id: UUID,
        runtime: SharedWorkspaceRuntime, isolation_identity: str,
        process: subprocess.Popen[bytes], master: int, status_read: int,
        guard: WorkspaceLeaseGuard, max_age_seconds: int,
        guest_home: str = "/home", simulated_uid: int | None = None,
        simulated_gid: int | None = None,
        ai_healthy: Callable[[], bool] | None = None,
    ) -> None:
        self.id = session_id
        self.workspace_id = workspace_id
        self.actor_id = actor_id
        self.runtime = runtime
        self.isolation_identity = isolation_identity
        self.process = process
        self.master = master
        self.guard = guard
        self.started_ns = time.time_ns()
        self.deadline = time.monotonic() + max_age_seconds
        self.guest_home = guest_home
        self.simulated_uid = simulated_uid
        self.simulated_gid = simulated_gid
        self._ai_healthy = ai_healthy
        self.detached = False
        self.closed = False
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._status_sha = hashlib.sha256()
        self._status_reader = threading.Thread(
            target=self._drain_status, args=(status_read,), daemon=True,
            name="potbot-bash-status",
        )
        self._status_reader.start()
        self._watchdog = threading.Thread(target=self._watch, daemon=True, name="potbot-bash-watch")
        self._watchdog.start()

    def _drain_status(self, fd: int) -> None:
        total = 0
        try:
            while True:
                fragment = os.read(fd, 4096)
                if not fragment:
                    return
                if total < 65536:
                    part = fragment[: 65536 - total]
                    self._status_sha.update(part)
                    total += len(part)
        except OSError:
            return
        finally:
            os.close(fd)

    def _watch(self) -> None:
        while not self._stop.wait(0.1):
            if self.process.poll() is not None:
                self.terminate()
                return
            if (self.guard.fenced or time.monotonic() >= self.deadline
                    or (self._ai_healthy is not None and not self._ai_healthy())):
                self.terminate(force=True)
                return

    def view(self) -> BashSessionView:
        with self._lock:
            return BashSessionView(
                self.id, self.workspace_id, self.actor_id,
                self.runtime.image_digest, self.isolation_identity,
                not self.closed and self.process.poll() is None,
                self.detached, self.process.poll(), self.started_ns,
                self.guest_home, self.simulated_uid, self.simulated_gid, False,
            )

    def require_running(self) -> None:
        if self.closed or self.guard.fenced or self.process.poll() is not None:
            raise StateMismatchError("Bash session is no longer available")

    def read(self, *, max_bytes: int = 16384, timeout: float = 0.0) -> bytes:
        if type(max_bytes) is not int or not 1 <= max_bytes <= 65536:
            raise InvalidInputError("PTY read size exceeds the allowed bound")
        if not isinstance(timeout, (float, int)) or not 0 <= timeout <= 1:
            raise InvalidInputError("PTY polling timeout is invalid")
        with self._lock:
            if self.closed:
                return b""
            fd = self.master
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return b""
        try:
            return os.read(fd, max_bytes)
        except (OSError, BlockingIOError):
            return b""

    def write(self, data: bytes) -> int:
        if not isinstance(data, bytes) or not 1 <= len(data) <= 8192:
            raise InvalidInputError("PTY input must contain 1-8192 bytes")
        with self._lock:
            self.require_running()
            return os.write(self.master, data)

    def resize(self, *, columns: int, rows: int) -> None:
        if any(type(v) is not int or not 10 <= v <= 500 for v in (columns, rows)):
            raise InvalidInputError("PTY dimensions must be between 10 and 500")
        with self._lock:
            self.require_running()
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    def terminate(self, *, force: bool = False) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self._stop.set()
            process = self.process
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=0.75)
            except subprocess.TimeoutExpired:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    process.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    pass
            stopped = process.poll() is not None
            os.close(self.master)
            self._status_reader.join(timeout=0.5)
            # A terminal may have written arbitrary files; this is only a
            # lifecycle receipt, not proof that its outputs were correct.
            statement = f"session:{self.id}:exit:{process.poll()}:isolation:{self.isolation_identity}"
            try:
                self.guard.finish(
                    evidence_sha256=hashlib.sha256(statement.encode()).hexdigest(),
                    uncertain=not stopped or self.guard.fenced,
                )
            except StateMismatchError:
                self.guard.abandon()  # Controller must reconcile an expired lease.


class SharedBashSessionSupervisor:
    """In-process PTY registry; all mutations are authorized by a trusted host.

    Shells hold an exclusive workspace-wide claim until termination because
    arbitrary interactive commands cannot be safely mapped to narrow write sets.
    """

    def __init__(
        self, *, ledger: WorkspaceLeaseLedger,
        backend: SharedWorkspaceBubblewrapBackend,
        authorize: Callable[[UUID, UUID], bool],
        max_sessions_per_workspace: int = 4,
        agent_homes: AgentHomeRegistry | None = None,
    ) -> None:
        if not isinstance(backend, SharedWorkspaceBubblewrapBackend):
            raise CapabilityUnavailableError("Bash requires qualified shared-rootfs isolation")
        if not callable(authorize):
            raise ScopeDeniedError("Bash requires a trusted host authorization callback")
        if type(max_sessions_per_workspace) is not int or not 1 <= max_sessions_per_workspace <= 16:
            raise InvalidInputError("invalid session limit")
        self.ledger = ledger
        self.backend = backend
        self.authorize = authorize
        self.max_sessions_per_workspace = max_sessions_per_workspace
        self.agent_homes = agent_homes
        self._lock = threading.RLock()
        self._sessions: dict[str, _BashSession] = {}

    def _owned(self, session_id: str, workspace_id: UUID, actor_id: UUID) -> _BashSession:
        if self.authorize(workspace_id, actor_id) is not True:
            raise ScopeDeniedError("Bash session permission denied")
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None or session.workspace_id != workspace_id or session.actor_id != actor_id:
            raise ScopeDeniedError("Bash session is unavailable to this workspace/principal")
        return session

    def open(
        self, *, workspace_id: UUID, actor_id: UUID,
        max_age_seconds: int = 1800, columns: int = 80, rows: int = 24,
    ) -> BashSessionView:
        if self.authorize(workspace_id, actor_id) is not True:
            raise ScopeDeniedError("Bash session creation is not authorized")
        if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 3600:
            raise InvalidInputError("Bash session lifetime must be at most one hour")
        if any(type(v) is not int or not 10 <= v <= 500 for v in (columns, rows)):
            raise InvalidInputError("invalid Bash terminal dimensions")
        runtime = self.backend.runtime
        validate_runtime_pin(self.ledger, runtime, workspace_id)
        home = Path(runtime.home).resolve(strict=True)
        if not home.is_dir():
            raise CapabilityUnavailableError("workspace home is not provisioned")
        agent_home = self.agent_homes.lookup(workspace_id, actor_id) if self.agent_homes else None
        if self.agent_homes is not None and agent_home is None:
            raise StateMismatchError("agent directory must be provisioned before opening Bash")
        working_directory = home if agent_home is None else home / agent_home.directory
        guest_home = "/home" if agent_home is None else agent_home.guest_path
        with self._lock:
            current = sum(s.workspace_id == workspace_id and not s.closed
                          for s in self._sessions.values())
            if current >= self.max_sessions_per_workspace:
                raise StateMismatchError("workspace terminal capacity reached")
            session_id = uuid4().hex
            guard = WorkspaceLeaseGuard.exclusive_home(
                self.ledger,
                WorkspaceExecutorIdentity(workspace_id, actor_id, runtime.image_digest),
                operation="bash.session", idempotency_key=f"bash-{session_id}",
            )
            guard.start()
            master = slave = status_read = status_write = -1
            launch = None
            process: subprocess.Popen[bytes] | None = None
            try:
                master, slave = os.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
                status_read, status_write = os.pipe()
                request = ProcessRequest(
                    argv=("/bin/bash", "--noprofile", "-i"),
                    cwd=working_directory, workspace_root=home, sandbox_root=home,
                    timeout_ms=max_age_seconds * 1000,
                    max_output_bytes=1048576,
                    memory_bytes=2 * 1024**3, cpu_seconds=max_age_seconds,
                    guest_uid=agent_home.uid if agent_home else None,
                    guest_gid=agent_home.gid if agent_home else None,
                )
                launch = self.backend.prepare(
                    argv=request.argv, workspace_root=request.workspace_root,
                    cwd=request.cwd, read_only_roots=(), writable_roots=(),
                    sandbox_root=home, status_fd=status_write,
                    memory_bytes=request.memory_bytes, cpu_seconds=request.cpu_seconds,
                    max_file_bytes=request.max_file_bytes,
                    max_open_files=request.max_open_files,
                    **({"guest_uid": request.guest_uid, "guest_gid": request.guest_gid}
                       if request.guest_uid is not None else {}),
                )
                process = subprocess.Popen(
                    launch.argv, stdin=slave, stdout=slave, stderr=slave,
                    cwd="/", env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                    close_fds=True, pass_fds=launch.pass_fds,
                    start_new_session=True,
                )
                os.close(slave)
                slave = -1
                os.close(status_write)
                status_write = -1
                for fd in launch.pass_fds:
                    if fd not in {status_write, status_read}:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                if process.poll() is not None:
                    raise CapabilityUnavailableError("Bash exited before session establishment")
                session = _BashSession(
                    session_id=session_id, workspace_id=workspace_id, actor_id=actor_id,
                    runtime=runtime, isolation_identity=launch.identity,
                    process=process, master=master, status_read=status_read,
                    guard=guard, max_age_seconds=max_age_seconds,
                    guest_home=guest_home,
                    simulated_uid=agent_home.uid if agent_home else None,
                    simulated_gid=agent_home.gid if agent_home else None,
                    ai_healthy=(self.backend.ai_monitor.is_healthy
                                if self.backend.ai_monitor is not None
                                else self.backend.operator_attachment.healthy
                                if self.backend.operator_attachment is not None else None),
                )
                self._sessions[session_id] = session
                return session.view()
            except BaseException:
                if process is not None and process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=2)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                for fd in {master, slave, status_read, status_write,
                           *(launch.pass_fds if launch is not None else ())}:
                    if fd >= 0:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                try:
                    guard.finish(
                        evidence_sha256=admission_digest("bash-open-failed"),
                        uncertain=process is not None and process.poll() is None,
                    )
                except StateMismatchError:
                    guard.abandon()
                raise

    def attach(self, *, session_id: str, workspace_id: UUID, actor_id: UUID) -> BashSessionView:
        session = self._owned(session_id, workspace_id, actor_id)
        with session._lock:
            session.require_running()
            session.detached = False
            return session.view()

    def detach(self, *, session_id: str, workspace_id: UUID, actor_id: UUID) -> BashSessionView:
        session = self._owned(session_id, workspace_id, actor_id)
        with session._lock:
            session.detached = True
            return session.view()

    def read(self, *, session_id: str, workspace_id: UUID, actor_id: UUID,
             max_bytes: int = 16384, timeout: float = 0.0) -> bytes:
        return self._owned(session_id, workspace_id, actor_id).read(
            max_bytes=max_bytes, timeout=timeout
        )

    def write(self, *, session_id: str, workspace_id: UUID, actor_id: UUID,
              data: bytes) -> int:
        return self._owned(session_id, workspace_id, actor_id).write(data)

    def resize(self, *, session_id: str, workspace_id: UUID, actor_id: UUID,
               columns: int, rows: int) -> None:
        self._owned(session_id, workspace_id, actor_id).resize(columns=columns, rows=rows)

    def terminate(self, *, session_id: str, workspace_id: UUID, actor_id: UUID) -> BashSessionView:
        session = self._owned(session_id, workspace_id, actor_id)
        session.terminate()
        return session.view()

    def list_sessions(self, *, workspace_id: UUID, actor_id: UUID) -> list[BashSessionView]:
        if self.authorize(workspace_id, actor_id) is not True:
            raise ScopeDeniedError("Bash session list is not authorized")
        with self._lock:
            sessions = [s.view() for s in self._sessions.values()
                        if s.workspace_id == workspace_id and s.actor_id == actor_id]
        return sorted(sessions, key=lambda s: s.started_ns)
