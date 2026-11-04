"""Live Linux checks for the U03 worker audit completion boundary."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.tools.worker_audit import process_tree
from potbot.tools.worker_telemetry import _STRACE_FLAGS, _cgroup_populated, _terminate_cgroup

_WORKLOAD = """
import os
import signal
import threading

thread = threading.Thread(target=lambda: None)
thread.start()
thread.join()
pid = os.fork()
if pid == 0:
    os.execl('/bin/true', 'true')
assert os.waitpid(pid, 0)[1] == 0
pid = os.fork()
if pid == 0:
    os.setsid()
    os.kill(os.getpid(), signal.SIGKILL)
assert os.waitpid(pid, 0)[1] == signal.SIGKILL
"""


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("strace") is None, reason="requires Linux strace"
)
def test_real_trace_records_thread_exec_and_signalled_child(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "strace",
            *_STRACE_FLAGS,
            "-o",
            str(tmp_path / "trace"),
            "--",
            sys.executable,
            "-I",
            "-c",
            _WORKLOAD,
        ],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    traces = sorted(tmp_path.glob("trace.*"))
    tree = process_tree(traces)
    assert tree["tree_complete"] is True
    assert {node["kind"] for node in tree["nodes"]} == {"root", "process", "thread"}
    killed = next(node for node in tree["nodes"] if node["exit"]["state"] == "signalled")
    assert killed["exit"]["signal"] == "SIGKILL"

    victim = tmp_path / f"trace.{killed['pid']}"
    lines = victim.read_text().splitlines(keepends=True)
    assert lines and "+++ killed by SIGKILL +++" in lines[-1]
    victim.write_text("".join(lines[:-1]))
    incomplete = process_tree(traces)
    assert incomplete["tree_complete"] is False
    assert incomplete["missing_exit_pids"] == [killed["pid"]]


@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("POTBOT_TEST_WORKER_CGROUP") != "1",
    reason="requires an explicitly authorized disposable Linux cgroup v2 fixture",
)
def test_real_nested_cgroup_requires_recursive_shutdown() -> None:
    parent = Path("/sys/fs/cgroup") / f"potbot-u03-test-{uuid4().hex}"
    child = parent / "nested"
    parent.mkdir()
    worker = None
    try:
        child.mkdir()
        worker = subprocess.Popen(
            ["/usr/bin/sleep", "30"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (child / "cgroup.procs").write_text(f"{worker.pid}\n")
        assert not (parent / "cgroup.procs").read_text().strip()
        assert _cgroup_populated(parent)
        assert _terminate_cgroup(parent, attempts=100, interval=0.05)
        assert worker.wait(timeout=5) == -signal.SIGKILL
        assert not _cgroup_populated(parent)
    finally:
        if worker is not None:
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=5)
        if child.exists():
            child.rmdir()
        parent.rmdir()
