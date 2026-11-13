"""Sealed ports and operator installation for the Python profiling worker."""

from __future__ import annotations

import re
from typing import Any

from potbot.domain.errors import ScopeDeniedError
from potbot.domain.serialization import content_hash
from potbot.tools.execution_contracts import ExecutionPolicy, WorkBinding, digest
from potbot.tools.python_profile_worker import EXECUTABLE, OPERATION, REQUEST, RESULT
from potbot.tools.sandbox_cog_worker import SandboxedCogOperation


def schemas() -> dict[str, dict[str, Any]]:
    from potbot.tools.semantic_contracts import HASH, array, obj

    repetitions = {"type": "integer", "minimum": 2, "maximum": 3}
    count = {"type": "integer", "minimum": 0}
    text = {"type": "string", "minLength": 1, "maxLength": 120}
    timing = obj({"cpu_ns": count, "wall_ns": count})
    function = obj(
        {name: count for name in ("line", "calls", "primitive_calls", "self_ns", "cumulative_ns")}
        | {"function": text}
    )
    sample = obj(
        {
            "python": text,
            "wall_ns": count,
            "cpu_ns": count,
            "result_sha256": HASH,
            "functions": array(function, 20),
            "function_count": count,
        }
    )
    return {
        REQUEST: obj({"repetitions": repetitions}),
        RESULT: obj(
            {
                "profile": {"const": "potbot.python-profile-comparison/1"},
                "source_sha256": HASH,
                "interpreter_sha256": HASH,
                "python": text,
                "result_sha256": HASH,
                "host": obj(
                    {
                        "system": text,
                        "kernel": text,
                        "architecture": text,
                        "logical_cpu_count": {"type": ["integer", "null"]},
                    }
                ),
                "clock": obj(
                    {
                        "wall_ns": {"const": "perf_counter_ns"},
                        "cpu_ns": {"const": "process_time_ns"},
                    }
                ),
                "profiler": obj(
                    {
                        "name": {"const": "cProfile"},
                        "clock": {"const": "process_time_ns"},
                        "threads": {"const": "main"},
                    }
                ),
                "limits": obj(
                    {
                        "per_process_wall_ms": {"const": 5000},
                        "per_process_cpu_seconds": {"const": 5},
                    }
                ),
                "warmup_pairs": {"const": 1},
                "repetitions": repetitions,
                "samples": obj({"baseline": array(sample, 3), "profile": array(sample, 3)}),
                "median_ns": obj({"baseline": timing, "profile": timing}),
                "overhead_ns": obj({"cpu_ns": {"type": "integer"}, "wall_ns": {"type": "integer"}}),
                "unavailable": array(text, 8),
            }
        ),
    }


def install_python_profile(
    filename: str,
    source_sha256: str,
    interpreter_sha256: str,
    executable_sha256: str,
    policy: ExecutionPolicy,
) -> SandboxedCogOperation:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\.py", filename) is None:
        raise ScopeDeniedError("Profiling requires a pinned script in the registered worker home")
    argv = (EXECUTABLE, filename, digest(source_sha256), digest(interpreter_sha256))
    implementation = content_hash({"argv": argv, "worker_sha256": digest(executable_sha256)})
    return SandboxedCogOperation(
        WorkBinding("script", OPERATION, REQUEST, RESULT, implementation, "read_only", policy),
        argv,
        executable_sha256,
    )


def require_python_profile(operation: SandboxedCogOperation) -> None:
    if len(operation.argv) != 4 or operation != install_python_profile(
        *operation.argv[1:],
        operation.executable_sha256,
        operation.binding.policy,
    ):
        raise ScopeDeniedError("Profiling requires the exact installed worker binding")
