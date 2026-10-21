"""Bounded subprocess supervision through the required isolation backend."""

from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError
from potbot.tools.isolation import BubblewrapBackend
from potbot.tools.secrets import CaptureRedactionError, CaptureRedactor

Outcome = Literal[
    "completed", "nonzero_exit", "timeout", "cancelled", "infrastructure_error", "inconclusive"
]
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_FORBIDDEN_ENV = frozenset({"LD_PRELOAD", "LD_AUDIT", "LD_LIBRARY_PATH", "BASH_ENV", "ENV"})


@dataclass(frozen=True, slots=True)
class ProcessRequest:
    argv: tuple[str, ...]
    cwd: Path
    workspace_root: Path
    timeout_ms: int = 60_000
    max_output_bytes: int = 1_048_576
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    read_only_roots: tuple[Path, ...] = ()
    stdin: bytes = field(default=b"", repr=False)
    memory_bytes: int = 4 * 1024**3
    cpu_seconds: int = 60
    max_file_bytes: int = 128 * 1024**2
    max_open_files: int = 256

    def __post_init__(self) -> None:
        if (
            not isinstance(self.argv, tuple)
            or not 1 <= len(self.argv) <= 256
            or any(
                not isinstance(value, str) or "\x00" in value or len(value) > 262144
                for value in self.argv
            )
            or not self.argv[0]
            or not Path(self.argv[0]).is_absolute()
        ):
            raise InvalidInputError("tool argv must be a bounded array with an absolute executable")
        for value, lower, upper in (
            (self.timeout_ms, 1, 3_600_000),
            (self.max_output_bytes, 1, 16_777_216),
            (self.memory_bytes, 16_777_216, 1024**4),
            (self.cpu_seconds, 1, 3600),
            (self.max_file_bytes, 1, 1024**4),
            (self.max_open_files, 32, 65536),
        ):
            if type(value) is not int or not lower <= value <= upper:
                raise InvalidInputError("process resource limits must be bounded positive integers")
        if not isinstance(self.stdin, bytes) or len(self.stdin) > 16_777_216:
            raise InvalidInputError("process input exceeds the supported bound")
        if len(self.environment) > 128 or any(
            not isinstance(name, str)
            or not _ENV_NAME.fullmatch(name)
            or name in _FORBIDDEN_ENV
            or not isinstance(value, str)
            or "\x00" in value
            or len(value.encode("utf-8")) > 65536
            for name, value in self.environment.items()
        ):
            raise InvalidInputError("process environment is not an allowed explicit mapping")


@dataclass(frozen=True, slots=True)
class ProcessResult:
    outcome: Outcome
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    elapsed_ms: int
    stdout_truncated: bool
    stderr_truncated: bool
    dispatched: bool
    isolation_identity: str | None
    error_code: str | None = None
    retryable: bool = False
    processes_stopped: bool = False


def _kill_group(process: subprocess.Popen[bytes], force: bool = False) -> None:
    # Once the direct child is reaped, do not signal a recycled process-group
    # identity. Bubblewrap's private PID namespace supplies descendant cleanup.
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        pass


class ProcessSupervisor:
    def __init__(self, backend: BubblewrapBackend | None = None) -> None:
        self.backend = backend or BubblewrapBackend()

    def run(
        self,
        request: ProcessRequest,
        *,
        cancelled: Callable[[], bool] | None = None,
        redactor: CaptureRedactor | None = None,
    ) -> ProcessResult:
        started = time.monotonic()
        redactor = redactor or CaptureRedactor()
        if not Path(request.argv[0]).is_file() or not os.access(request.argv[0], os.X_OK):
            return ProcessResult(
                "infrastructure_error",
                None,
                b"",
                b"executable unavailable",
                0,
                False,
                False,
                False,
                None,
                "source_unavailable",
                processes_stopped=True,
            )
        status_read, status_write = os.pipe()
        launch = None
        process: subprocess.Popen[bytes] | None = None
        try:
            launch = self.backend.prepare(
                argv=request.argv,
                workspace_root=request.workspace_root,
                cwd=request.cwd,
                read_only_roots=request.read_only_roots,
                status_fd=status_write,
                memory_bytes=request.memory_bytes,
                cpu_seconds=request.cpu_seconds,
                max_file_bytes=request.max_file_bytes,
                max_open_files=request.max_open_files,
            )
            environment = {
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                **request.environment,
            }
            process = subprocess.Popen(
                launch.argv,
                cwd="/",
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
                pass_fds=launch.pass_fds,
            )
        except (CapabilityUnavailableError, OSError):
            return ProcessResult(
                "infrastructure_error",
                None,
                b"",
                b"required isolation unavailable",
                int((time.monotonic() - started) * 1000),
                False,
                False,
                False,
                None,
                "capability_unavailable",
                processes_stopped=True,
            )
        finally:
            os.close(status_write)
            if launch is not None:
                os.close(launch.pass_fds[0])
            if process is None:
                os.close(status_read)
        assert (
            process.stdout is not None and process.stderr is not None and process.stdin is not None
        )
        stop_poll = threading.Event()
        cancel_seen = threading.Event()
        poll_failed = threading.Event()
        poll_thread: threading.Thread | None = None
        if cancelled is not None:

            def poll_cancel() -> None:
                while not stop_poll.is_set():
                    try:
                        if cancelled():
                            cancel_seen.set()
                            return
                    except Exception:
                        poll_failed.set()
                        return
                    stop_poll.wait(0.05)

            poll_thread = threading.Thread(
                target=poll_cancel, name="potbot-cancel-poll", daemon=True
            )
            poll_thread.start()
        streams = {"stdout": bytearray(), "stderr": bytearray(), "status": bytearray()}
        totals = {"stdout": 0, "stderr": 0}
        limits = {
            "stdout": request.max_output_bytes + redactor.overlap_bytes,
            "stderr": request.max_output_bytes + redactor.overlap_bytes,
            "status": 65536,
        }
        outcome: Outcome | None = None
        terminated_at: float | None = None
        input_offset = 0
        processes_stopped = False
        selector = selectors.DefaultSelector()
        handles = [process.stdout, process.stderr]
        try:
            for handle, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(handle.fileno(), False)
                selector.register(handle, selectors.EVENT_READ, name)
            os.set_blocking(status_read, False)
            selector.register(status_read, selectors.EVENT_READ, "status")
            if request.stdin:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                process.stdin.close()
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                if outcome is None:
                    if (now - started) * 1000 >= request.timeout_ms:
                        outcome = "timeout"
                    elif cancel_seen.is_set():
                        outcome = "cancelled"
                    elif poll_failed.is_set():
                        outcome = "inconclusive"
                    if outcome is not None:
                        terminated_at = now
                        _kill_group(process)
                if terminated_at is not None and now - terminated_at >= 0.25:
                    _kill_group(process, force=True)
                if terminated_at is not None and now - terminated_at > 2:
                    break
                for key, _ in selector.select(0.025):
                    name = key.data
                    fd = key.fd
                    if name == "stdin":
                        try:
                            input_offset += os.write(
                                fd, request.stdin[input_offset : input_offset + 65536]
                            )
                        except BrokenPipeError:
                            input_offset = len(request.stdin)
                        except BlockingIOError:
                            continue
                        if input_offset >= len(request.stdin):
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                        continue
                    try:
                        data = os.read(fd, 65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    if name in totals:
                        totals[name] += len(data)
                    available = max(0, limits[name] - len(streams[name]))
                    streams[name].extend(data[:available])
            _kill_group(process, force=True)  # Also terminate any descendants after a normal exit.
            try:
                return_code = process.wait(timeout=2)
                processes_stopped = True
            except subprocess.TimeoutExpired:
                return_code = None
                outcome = "inconclusive"
        finally:
            stop_poll.set()
            if poll_thread is not None:
                poll_thread.join(timeout=0.1)
            _kill_group(process, force=True)
            try:
                return_code = process.wait(timeout=2)
                processes_stopped = True
            except subprocess.TimeoutExpired:
                processes_stopped = False
                return_code = None
                outcome = "inconclusive"
            selector.close()
            for handle in handles:
                handle.close()
            if not process.stdin.closed:
                process.stdin.close()
            os.close(status_read)
        statuses = []
        for line in bytes(streams["status"]).splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    statuses.append(value)
            except (ValueError, UnicodeDecodeError):
                pass
        # Bubblewrap emits child-pid before mount setup, but emits exit-code
        # only through its command supervisor. A failed namespace/mount setup
        # must not be mistaken for an ordinary nonzero program result.
        command_started = any("exit-code" in value for value in statuses)
        dispatched = command_started or (
            outcome is not None and any("child-pid" in value for value in statuses)
        )
        error_code = None
        if outcome is None:
            if not dispatched:
                outcome = "infrastructure_error"
                error_code = "capability_unavailable"
            else:
                outcome = "completed" if return_code == 0 else "nonzero_exit"
        elif outcome in {"timeout", "cancelled"}:
            error_code = outcome
        elif outcome == "inconclusive":
            error_code = "indeterminate"
        captures: dict[str, bytes] = {}
        try:
            for name in ("stdout", "stderr"):
                captures[name] = redactor.redact_bytes(
                    bytes(streams[name]), complete=totals[name] <= len(streams[name])
                )[: request.max_output_bytes]
        except CaptureRedactionError:
            captures = {"stdout": b"", "stderr": b"capture publication policy failed"}
            outcome = "inconclusive"
            error_code = "tool_failure"
        return ProcessResult(
            outcome,
            return_code,
            captures["stdout"],
            captures["stderr"],
            int((time.monotonic() - started) * 1000),
            totals["stdout"] > request.max_output_bytes,
            totals["stderr"] > request.max_output_bytes,
            dispatched,
            launch.identity,
            error_code,
            processes_stopped=processes_stopped,
        )
