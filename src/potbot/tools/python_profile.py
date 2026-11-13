"""Bounded cProfile comparisons for a host-approved, self-contained Python workload."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from potbot.domain.errors import (
    DomainError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.tools.python_profile_worker import (
    CAPTURE,
    CPU_SECONDS,
    REPORT_LIMIT,
    SOURCE_LIMIT,
    TIMEOUT_MS,
    report,
    summarize,
)
from potbot.tools.supervisor import ProcessRequest, ProcessSupervisor
from potbot.tools.temporary import TemporaryDirectory


def _read_source(source: Path) -> bytes:
    # Avoid opening a symlink or blocking on a substituted FIFO between path
    # validation and the pinned read. The digest selects the exact bytes run.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(source, flags), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ScopeDeniedError("Profiling source must be a regular file")
        workload = stream.read(SOURCE_LIMIT + 1)
    if not 0 < len(workload) <= SOURCE_LIMIT:
        raise InvalidInputError("Profiling source exceeds the supported size")
    return workload


def compare_python_profile(
    supervisor: ProcessSupervisor,
    *,
    workspace: Path,
    source: Path,
    source_sha256: str,
    interpreter: Path,
    interpreter_sha256: str,
    repetitions: int = 3,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run authorized repeatable source in new isolated processes, alternating order.

    The trusted controller selects a dedicated workspace, pinned runtime and
    source. The script must be safe to repeat and publish a deterministic JSON
    RESULT. Measurements describe its main thread's Python call profile and
    per-process CPU/wall time. Measurements are evidence from the admitted
    workload and do not constitute independent attestation of its behavior.
    """
    if type(repetitions) is not int or not 2 <= repetitions <= 5:
        raise InvalidInputError("Profiling requires two to five measured repetitions")
    if (
        not workspace.is_absolute()
        or not workspace.is_dir()
        or workspace.resolve() != workspace
        or not source.is_absolute()
        or source.resolve() != source
        or not source.is_relative_to(workspace)
        or not source.is_file()
        or not interpreter.is_absolute()
        or interpreter.resolve() != interpreter
        or not interpreter.is_file()
    ):
        raise ScopeDeniedError("Profiling requires canonical workspace source and interpreter")
    workload = _read_source(source)
    with interpreter.open("rb") as stream:
        runtime_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    if (
        hashlib.sha256(workload).hexdigest() != source_sha256
        or runtime_sha256 != interpreter_sha256
    ):
        raise ScopeDeniedError("Profiling source or interpreter differs from its admitted digest")

    samples: dict[str, list[dict[str, Any]]] = {"baseline": [], "profile": []}
    result_sha256 = None
    runtime = None
    for iteration in range(repetitions + 1):
        order = ("baseline", "profile") if iteration % 2 == 0 else ("profile", "baseline")
        for mode in order:
            scratch = TemporaryDirectory(dir=workspace, prefix="profile-")
            run_root = scratch.path
            result = supervisor.run(
                ProcessRequest(
                    (str(interpreter), "-I", "-B", "-c", CAPTURE, mode),
                    run_root,
                    run_root,
                    sandbox_root=workspace,
                    stdin=workload,
                    timeout_ms=TIMEOUT_MS,
                    cpu_seconds=CPU_SECONDS,
                    memory_bytes=256 * 1024**2,
                    max_file_bytes=1024**2,
                    max_output_bytes=REPORT_LIMIT,
                ),
                cancelled=cancelled,
            )
            if result.processes_stopped or not result.dispatched:
                scratch.cleanup()
            if (
                result.outcome != "completed"
                or not result.dispatched
                or not result.processes_stopped
                or not result.isolation_identity
                or result.stdout_truncated
                or result.stderr
                or result.stderr_truncated
            ):
                raise StateMismatchError(
                    f"Profiling process did not complete cleanly ({result.outcome})"
                )
            try:
                row = report(result.stdout, profiled=mode == "profile")
            except ValueError as error:
                raise StateMismatchError(str(error)) from error
            current = (row["result_sha256"], row["python"])
            if result_sha256 is None:
                result_sha256, runtime = current
            if current != (result_sha256, runtime):
                raise StateMismatchError("Profiling interpreter or workload result changed")
            if iteration:
                samples[mode].append({**row, "isolation_identity": result.isolation_identity})

    return summarize(samples, source_sha256, runtime_sha256, repetitions)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--interpreter", required=True, type=Path)
    parser.add_argument("--interpreter-sha256", required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    options = parser.parse_args()
    try:
        result = compare_python_profile(ProcessSupervisor(), **vars(options))
    except DomainError as error:
        print(json.dumps(error.to_dict(), sort_keys=True), file=sys.stderr)
        return 2
    except OSError:
        print("Profiling source, interpreter or scratch storage unavailable", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
