#!/usr/bin/python3 -I
"""Pinned stdio worker for an admitted Python CPU comparison."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

SOURCE_LIMIT = 65536
REPORT_LIMIT = 32768
TIMEOUT_MS = 5000
CPU_SECONDS = 5
_FUNCTION_LIMIT = 20
EXECUTABLE = "/usr/local/libexec/potbot-python-profile"
REQUEST = "syn://type/potbot-python-profile-request-v1"
RESULT = "syn://type/potbot-python-profile-result-v1"
OPERATION = "syn://thing/potbot-python-profile-v1"

# Run the exact source received from the controller. Keep target output and
# native profiler dumps out of the wire format. The trace contains only rows
# attributable to the selected source; external code can contribute to cumulative time.
CAPTURE = r"""
import contextlib
import cProfile
import hashlib
import json
import os
import pstats
import resource
import sys
import time

if sys.version_info[:2] != (3, 12):
    raise SystemExit("Unsupported profiling interpreter")
soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
if soft == resource.RLIM_INFINITY or soft > 5:
    resource.setrlimit(resource.RLIMIT_CPU, (5, hard))
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


def report(stdout: bytes, *, profiled: bool) -> dict[str, Any]:
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
        raise ValueError("Profiling process returned an invalid bounded report") from error
    return row


def summarize(
    samples: dict[str, list[dict[str, Any]]],
    source_sha256: str,
    interpreter_sha256: str,
    repetitions: int,
) -> dict[str, Any]:
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
        "interpreter_sha256": interpreter_sha256,
        "python": samples["baseline"][0]["python"],
        "result_sha256": samples["baseline"][0]["result_sha256"],
        "host": {
            "system": platform.system(),
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "logical_cpu_count": os.cpu_count(),
        },
        "clock": {"wall_ns": "perf_counter_ns", "cpu_ns": "process_time_ns"},
        "profiler": {"name": "cProfile", "clock": "process_time_ns", "threads": "main"},
        "limits": {"per_process_wall_ms": TIMEOUT_MS, "per_process_cpu_seconds": CPU_SECONDS},
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


def _sample(source: bytes, interpreter: Path, mode: str) -> dict[str, Any]:
    # Linux guest scratch follows the worker's /tmp mount. A fresh directory
    # is claimed exclusively for each sample and is never selected by the input.
    directory = Path("/tmp") / ("potbot-profile-" + uuid4().hex)
    directory.mkdir(mode=0o700)
    try:
        # The outer worker owns the process tree and the file/output ceilings.
        # Spooling avoids buffering an unbounded child response in memory.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            result = subprocess.run(
                [str(interpreter), "-I", "-B", "-c", CAPTURE, mode],
                input=source,
                stdout=output,
                stderr=errors,
                cwd=directory,
                timeout=TIMEOUT_MS / 1000,
                check=False,
            )
            if result.returncode or errors.tell():
                raise ValueError("Profile sample failed")
            output.seek(0)
            raw = output.read(REPORT_LIMIT + 1)
            if len(raw) > REPORT_LIMIT:
                raise ValueError("Profile sample exceeded its output bound")
    finally:
        shutil.rmtree(directory)
    return report(raw, profiled=mode == "profile")


def _unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(items)
    if len(result) != len(items):
        raise ValueError("Duplicate profiling request key")
    return result


def main() -> int:
    try:
        _, filename, source_sha, python_sha = sys.argv
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\.py", filename) is None or any(
            re.fullmatch(r"[0-9a-f]{64}", pin) is None for pin in (source_sha, python_sha)
        ):
            raise ValueError
        raw = sys.stdin.buffer.read(2049)
        if len(raw) > 2048:
            raise ValueError
        wire = json.loads(raw, object_pairs_hook=_unique)
        port = wire["input"]
        repetitions = port["value"]["repetitions"]
        if (
            set(wire) != {"schema_version", "operation_ref", "input", "invocation_id"}
            or type(wire["schema_version"]) is not int
            or wire["schema_version"] != 1
            or wire["operation_ref"] != OPERATION
            or str(UUID(wire["invocation_id"])) != wire["invocation_id"]
            or port != {"type_ref": REQUEST, "value": {"repetitions": repetitions}}
            or type(repetitions) is not int
            or not 2 <= repetitions <= 3
        ):
            raise ValueError
        interpreter = Path(sys.executable).resolve(strict=True)
        with interpreter.open("rb") as executable:
            if hashlib.file_digest(executable, "sha256").hexdigest() != python_sha:
                raise ValueError
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(filename, flags), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError
            source = stream.read(SOURCE_LIMIT + 1)
        if not 0 < len(source) <= SOURCE_LIMIT or hashlib.sha256(source).hexdigest() != source_sha:
            raise ValueError

        samples: dict[str, list[dict[str, Any]]] = {"baseline": [], "profile": []}
        selected = None
        for iteration in range(repetitions + 1):
            order = ("baseline", "profile") if iteration % 2 == 0 else ("profile", "baseline")
            for mode in order:
                row = _sample(source, interpreter, mode)
                current = (row["python"], row["result_sha256"])
                if selected is None:
                    selected = current
                if current != selected:
                    raise ValueError
                if iteration:
                    samples[mode].append(row)
        value = summarize(samples, source_sha, python_sha, repetitions)
        # Bind stdout to the generic worker's 16 KiB envelope, including JSON escaping.
        response = json.dumps(
            {"schema_version": 1, "type_ref": RESULT, "value": value},
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        if len(response) > 16_000:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, RecursionError, subprocess.TimeoutExpired):
        print("Pinned Python profiling failed", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
