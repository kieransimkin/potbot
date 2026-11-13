"""Bounded cProfile comparisons for a host-approved, self-contained Python workload."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import stat
import statistics
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
from potbot.tools.supervisor import ProcessRequest, ProcessSupervisor
from potbot.tools.temporary import TemporaryDirectory

_SOURCE_LIMIT = 65_536
_REPORT_LIMIT = 32_768
_FUNCTION_LIMIT = 20
_TIMEOUT_MS = 5_000
_CPU_SECONDS = 5

# Run the exact source received from the controller. Keep target output and
# native profiler dumps out of the wire format. The trace contains only rows
# attributable to the selected source; external code can contribute to cumulative time.
_CAPTURE = r"""
import contextlib
import cProfile
import hashlib
import json
import os
import pstats
import sys
import time

if sys.version_info[:2] != (3, 12):
    raise SystemExit("Unsupported profiling interpreter")
filename = "potbot-approved-workload.py"
code = compile(sys.stdin.buffer.read(65537), filename, "exec")
namespace = {"__name__": "__main__", "__file__": filename}
profiler = cProfile.Profile(timer=time.process_time_ns, timeunit=1e-9)
profiled = sys.argv[1] == "profile"
with open(os.devnull, "w") as discarded:
    with contextlib.redirect_stdout(discarded), contextlib.redirect_stderr(discarded):
        wall, cpu = time.perf_counter_ns(), time.process_time_ns()
        if profiled:
            profiler.enable()
        try:
            exec(code, namespace)
        finally:
            if profiled:
                profiler.disable()
        cpu, wall = time.process_time_ns() - cpu, time.perf_counter_ns() - wall

encoded = json.dumps(namespace["RESULT"], sort_keys=True, separators=(",", ":"), allow_nan=False)
if len(encoded) > 16384:
    raise SystemExit("Profiling result exceeds bound")
report = {
    "python": sys.version.split()[0],
    "wall_ns": wall,
    "cpu_ns": cpu,
    "result_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
    "functions": [],
    "function_count": 0,
}
if profiled:
    rows = [
        {"line": line, "function": name[:120], "calls": calls, "primitive_calls": primitive,
         "self_ns": round(own * 1e9), "cumulative_ns": round(cumulative * 1e9)}
        for (path, line, name), (primitive, calls, own, cumulative, _) in
        pstats.Stats(profiler).stats.items() if path == filename
    ]
    rows.sort(key=lambda row: (-row["self_ns"], row["line"], row["function"]))
    report["function_count"] = len(rows)
    report["functions"] = rows[:20]
print(json.dumps(report, separators=(",", ":"), allow_nan=False))
"""


def _report(stdout: bytes, *, profiled: bool) -> dict[str, Any]:
    fields = {"python", "wall_ns", "cpu_ns", "result_sha256", "functions", "function_count"}
    function_fields = {"line", "function", "calls", "primitive_calls", "self_ns", "cumulative_ns"}
    try:
        row = json.loads(stdout)
        if (
            not isinstance(row, dict)
            or set(row) != fields
            or not isinstance(row["python"], str)
            or re.fullmatch(r"3\.12\.[0-9]{1,3}", row["python"]) is None
            or not isinstance(row["result_sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", row["result_sha256"]) is None
            or any(type(row[key]) is not int or row[key] <= 0 for key in ("wall_ns", "cpu_ns"))
            or type(row["function_count"]) is not int
            or row["function_count"] < 0
            or not isinstance(row["functions"], list)
            or len(row["functions"]) != min(_FUNCTION_LIMIT, row["function_count"])
            or (row["function_count"] == 0) == profiled
        ):
            raise ValueError
        for item in row["functions"]:
            if (
                not isinstance(item, dict)
                or set(item) != function_fields
                or not isinstance(item["function"], str)
                or not 1 <= len(item["function"]) <= 120
                or any(
                    type(item[key]) is not int or item[key] < 0
                    for key in function_fields - {"function"}
                )
                or not 0 < item["primitive_calls"] <= item["calls"]
                or item["self_ns"] > item["cumulative_ns"]
            ):
                raise ValueError
    except (UnicodeError, ValueError, RecursionError) as error:
        raise StateMismatchError("Profiling process returned an invalid bounded report") from error
    return row


def _read_source(source: Path) -> bytes:
    # Avoid opening a symlink or blocking on a substituted FIFO between path
    # validation and the pinned read. The digest selects the exact bytes run.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(source, flags), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ScopeDeniedError("Profiling source must be a regular file")
        workload = stream.read(_SOURCE_LIMIT + 1)
    if not 0 < len(workload) <= _SOURCE_LIMIT:
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
                    (str(interpreter), "-I", "-B", "-c", _CAPTURE, mode),
                    run_root,
                    run_root,
                    sandbox_root=workspace,
                    stdin=workload,
                    timeout_ms=_TIMEOUT_MS,
                    cpu_seconds=_CPU_SECONDS,
                    memory_bytes=256 * 1024**2,
                    max_file_bytes=1024**2,
                    max_output_bytes=_REPORT_LIMIT,
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
            row = _report(result.stdout, profiled=mode == "profile")
            current = (row["result_sha256"], row["python"])
            if result_sha256 is None:
                result_sha256, runtime = current
            if current != (result_sha256, runtime):
                raise StateMismatchError("Profiling interpreter or workload result changed")
            if iteration:
                samples[mode].append({**row, "isolation_identity": result.isolation_identity})

    elapsed = {
        mode: {
            metric: round(statistics.median(row[metric] for row in rows))
            for metric in ("cpu_ns", "wall_ns")
        }
        for mode, rows in samples.items()
    }

    return {
        "profile": "potbot.python-profile-comparison/1",
        "source_sha256": source_sha256,
        "interpreter_sha256": runtime_sha256,
        "python": runtime,
        "result_sha256": result_sha256,
        "host": {
            "system": platform.system(),
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "logical_cpu_count": os.cpu_count(),
        },
        "clock": {"wall_ns": "perf_counter_ns", "cpu_ns": "process_time_ns"},
        "profiler": {"name": "cProfile", "clock": "process_time_ns", "threads": "main"},
        "limits": {"per_process_wall_ms": _TIMEOUT_MS, "per_process_cpu_seconds": _CPU_SECONDS},
        "warmup_pairs": 1,
        "repetitions": repetitions,
        "samples": samples,
        "median_ns": elapsed,
        "overhead_ns": {
            metric: elapsed["profile"][metric] - elapsed["baseline"][metric]
            for metric in ("cpu_ns", "wall_ns")
        },
        "unavailable": [
            "target_ram_bytes",
            "gpu",
            "energy",
            "thermal_state",
            "background_load",
            "sampling_uncertainty",
        ],
    }


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
