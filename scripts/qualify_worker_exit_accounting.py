"""Live operator-side Linux taskstats qualification gate (no database required).

Run under the SAME privileges/kernel as the Potbot operator, not in a Cog
sandbox. The command returns nonzero when it cannot register on all CPUs or
observe the exit resources of every test child. Never use an absent listener as
permission to run unmonitored workers.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

from potbot.tools.taskstats_exit import TaskstatsExitStream, check_taskstats_layout


def qualify(count: int = 12) -> dict[str, object]:
    if not 1 <= count <= 100:
        raise ValueError("fixture size must be between 1 and 100")
    check_taskstats_layout()
    stream = TaskstatsExitStream()
    processes: list[subprocess.Popen[bytes]] = []
    try:
        stream.open()  # Entire online CPU mask registered BEFORE ANY child starts.
        for _ in range(count):
            processes.append(subprocess.Popen([sys.executable, "-c",
                "p=bytearray(1<<20); p[17]=1; print(sum(p), flush=True)"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        for proc in processes:
            if proc.wait(timeout=20) != 0:
                raise RuntimeError("accounting fixture child did not exit cleanly")
        results = stream.coverage({p.pid for p in processes}, deadline_seconds=5)
        if len(results) != count or any(v.elapsed_usec <= 0 or v.rss_peak_bytes <= 0
                                        or v.cpu_user_usec + v.cpu_system_usec <= 0
                                        for v in results.values()):
            raise RuntimeError("kernel taskstats did not provide valid per-task lifetime counters")
        return {"schema": 1, "qualified": True, "exited_tasks": count,
                "source": "linux-generic-netlink-taskstats", "unaccounted_tasks": 0,
                "kernel_uapi": "taskstats-v10-or-later"}
    finally:
        for child in processes:
            if child.poll() is None:
                child.kill()
            child.wait()
        stream.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--children", type=int, default=12)
    args = parser.parse_args()
    try:
        result = qualify(args.children)
    except Exception as exc:
        # No task PIDs, paths, or syscall arguments emitted on failure.
        print(json.dumps({"schema": 1, "qualified": False, "error": type(exc).__name__}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
