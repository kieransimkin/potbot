"""T02.3: bounded interactive subprocesses under Potbot's existing isolation backend.

An MCP server needs multiple request/response exchanges over one pair of pipes.
The existing ProcessSupervisor.run deliberately handles *one-shot* commands;
this companion reuses its ProcessRequest and BubblewrapBackend.prepare, including
its private namespaces, seccomp rules, read-only runtimes, workspace mounts and
resource limits. It NEVER falls back to unconfined host execution.

This layer neither installs executables nor authorizes their launch. The trusted
controller must perform the same durable permission/budget/lease admission as
ToolGateway before invoking it. A returned process handle is not an execution
receipt or proof of isolation beyond the selected backend's source policy.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from potbot.domain.errors import CapabilityUnavailableError, IndeterminateExecutionError
from potbot.tools.supervisor import ProcessRequest, ProcessSupervisor

_MAX_STATUS_BYTES = 65_536
_SHUTDOWN_GRACE_S = 0.5


@dataclass(frozen=True, slots=True)
class InteractiveExit:
    """Bounded metadata only. Raw stderr is deliberately never published."""

    return_code: int | None
    isolation_identity: str
    stderr_bytes: int
    stderr_truncated: bool
    status_sha256: str | None
    forced_termination: bool
    processes_stopped: bool


class IsolatedInteractiveProcess:
    """A single-use, fail-closed process lifetime; caller owns async close()."""

    def __init__(
        self,
        request: ProcessRequest,
        process: asyncio.subprocess.Process,
        isolation_identity: str,
        status_fd: int,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        import hashlib

        self.request = request
        self.process = process
        self.isolation_identity = isolation_identity
        self._hash_constructor = hashlib.sha256
        self._status_fd = status_fd
        self._started_at = time.monotonic()
        self._closed = False
        self._forced = False
        self._stderr_bytes = 0
        self._status_hash: str | None = None
        self._stderr_truncated = False
        self._status_task = asyncio.create_task(self._drain_status(), name="potbot-mcp-status")
        self._stderr_task = asyncio.create_task(self._drain_stderr(), name="potbot-mcp-stderr")
        self._watchdog = asyncio.create_task(self._deadline(), name="potbot-mcp-watchdog")
        self._cancel_watcher = (
            asyncio.create_task(self._poll_cancel(cancelled), name="potbot-mcp-cancel")
            if cancelled is not None
            else None
        )
        self._close_lock = asyncio.Lock()

    @property
    def reader(self) -> asyncio.StreamReader:
        if self.process.stdout is None:
            raise IndeterminateExecutionError("Isolated MCP stdout is unavailable")
        return self.process.stdout

    @property
    def writer(self) -> asyncio.StreamWriter:
        if self.process.stdin is None:
            raise IndeterminateExecutionError("Isolated MCP stdin is unavailable")
        return self.process.stdin

    @property
    def closed(self) -> bool:
        return self._closed

    def remaining_seconds(self) -> float:
        return max(0.0, self.request.timeout_ms / 1000 - (time.monotonic() - self._started_at))

    async def _deadline(self) -> None:
        try:
            await asyncio.sleep(self.request.timeout_ms / 1000)
            await self.close(force=True)
        except asyncio.CancelledError:
            return

    async def _poll_cancel(self, cancelled: Callable[[], bool]) -> None:
        """Terminate on task cancellation or failure of its authoritative checker."""
        try:
            while not self._closed:
                try:
                    if await asyncio.to_thread(cancelled):
                        await self.close(force=True)
                        return
                except Exception:
                    await self.close(force=True)
                    return
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            return

    async def _drain_stderr(self) -> None:
        reader = self.process.stderr
        if reader is None:
            return
        while chunk := await reader.read(65_536):
            self._stderr_bytes += len(chunk)
            # Stderr can contain raw credentials or attacker-authored text.
            # Only bounded counts, not raw bytes, cross this boundary.
            if self._stderr_bytes > self.request.max_output_bytes:
                self._stderr_truncated = True

    async def _drain_status(self) -> None:
        """Keep Bubblewrap's status pipe drained without blocking the event loop."""
        loop = asyncio.get_running_loop()
        fd = self._status_fd
        os.set_blocking(fd, False)
        buffer = bytearray()
        done: asyncio.Future[None] = loop.create_future()

        def ready() -> None:
            try:
                while True:
                    try:
                        fragment = os.read(fd, 4096)
                    except BlockingIOError:
                        return
                    if not fragment:
                        if not done.done():
                            done.set_result(None)
                        return
                    if len(buffer) < _MAX_STATUS_BYTES:
                        buffer.extend(fragment[: _MAX_STATUS_BYTES - len(buffer)])
                    # Deliberately discard all excess data while still draining.
            except OSError:
                if not done.done():
                    done.set_result(None)

        try:
            loop.add_reader(fd, ready)
            ready()
            await done
        finally:
            loop.remove_reader(fd)
            os.close(fd)
            if buffer:
                self._status_hash = self._hash_constructor(bytes(buffer)).hexdigest()

    def _signal(self, *, force: bool) -> None:
        if self.process.returncode is not None:
            return  # Prevent signalling a recycled process group.
        try:
            os.killpg(self.process.pid, signal.SIGKILL if force else signal.SIGTERM)
            if force:
                self._forced = True
        except ProcessLookupError:
            pass

    async def close(self, *, force: bool = False) -> InteractiveExit:
        async with self._close_lock:
            if not self._closed:
                self._closed = True
                if self.process.stdin is not None:
                    self.process.stdin.close()
                if force:
                    self._signal(force=True)
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=_SHUTDOWN_GRACE_S)
                except TimeoutError:
                    self._signal(force=False)
                    try:
                        await asyncio.wait_for(self.process.wait(), timeout=_SHUTDOWN_GRACE_S)
                    except TimeoutError:
                        self._signal(force=True)
                        try:
                            await asyncio.wait_for(self.process.wait(), timeout=2.0)
                        except TimeoutError:
                            pass
                # The status fd has to stay open until the child is reaped.
                current = asyncio.current_task()
                if self._watchdog is not current:
                    self._watchdog.cancel()
                if self._cancel_watcher is not None and self._cancel_watcher is not current:
                    self._cancel_watcher.cancel()
                for task in (self._stderr_task, self._status_task):
                    try:
                        await asyncio.wait_for(task, timeout=1.0)
                    except (TimeoutError, asyncio.CancelledError):
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
        return InteractiveExit(
            return_code=self.process.returncode,
            isolation_identity=self.isolation_identity,
            stderr_bytes=self._stderr_bytes,
            stderr_truncated=self._stderr_truncated,
            status_sha256=self._status_hash,
            forced_termination=self._forced,
            processes_stopped=self.process.returncode is not None,
        )

    async def __aenter__(self) -> IsolatedInteractiveProcess:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


class InteractiveProcessSupervisor(ProcessSupervisor):
    """Interactive extension of ProcessSupervisor; same qualified isolation backend."""

    async def open(
        self, request: ProcessRequest, *, cancelled: Callable[[], bool] | None = None
    ) -> IsolatedInteractiveProcess:
        if not isinstance(request, ProcessRequest):
            raise CapabilityUnavailableError("Interactive execution requires ProcessRequest")
        if not os.path.isfile(request.argv[0]) or not os.access(request.argv[0], os.X_OK):
            raise CapabilityUnavailableError("Pinned interactive executable is unavailable")
        status_read, status_write = os.pipe()
        launch: Any = None
        process: asyncio.subprocess.Process | None = None
        try:
            launch = self.backend.prepare(
                argv=request.argv,
                workspace_root=request.workspace_root,
                cwd=request.cwd,
                read_only_roots=request.read_only_roots,
                writable_roots=request.writable_roots,
                sandbox_root=request.sandbox_root or request.workspace_root,
                status_fd=status_write,
                memory_bytes=request.memory_bytes,
                cpu_seconds=request.cpu_seconds,
                max_file_bytes=request.max_file_bytes,
                max_open_files=request.max_open_files,
            )
            # Do not inherit environment or CWD from the model/controller host.
            environment = {
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                **request.environment,
            }
            process = await asyncio.create_subprocess_exec(
                *launch.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd="/",
                env=environment,
                start_new_session=True,
                pass_fds=launch.pass_fds,
                limit=min(request.max_output_bytes + 2, 2 * 1024 * 1024 + 2),
            )
        finally:
            # Closing the seccomp/status write descriptors in the parent is vital:
            # otherwise Bubblewrap's status pipe never reaches EOF.
            for fd in {status_write, *(launch.pass_fds if launch is not None else ())}:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if process is None:
                os.close(status_read)
        assert launch is not None and process is not None
        return IsolatedInteractiveProcess(
            request, process, launch.identity, status_read, cancelled=cancelled
        )
