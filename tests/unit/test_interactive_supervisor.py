"""T02.3 offline contract tests: a fake backend is NEVER a security qualification."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from potbot.domain.errors import CapabilityUnavailableError
from potbot.tools.supervisor import ProcessRequest

from potbot.tools.interactive_supervisor import InteractiveProcessSupervisor


class LocalOnlyFixtureBackend:
    """Deliberately unconfined; restricted to authored unit fixtures."""

    def prepare(self, *, argv, status_fd, **_):
        return SimpleNamespace(
            argv=argv, pass_fds=(status_fd,), identity="unit-fixture-NOT-isolation"
        )


class RejectBackend:
    def prepare(self, **_):
        raise CapabilityUnavailableError("No sandbox qualified")


def request(tmp_path: Path, script: Path, *, timeout: int = 1500) -> ProcessRequest:
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    return ProcessRequest(
        argv=(sys.executable, "-u", str(script)),
        cwd=root,
        workspace_root=root,
        sandbox_root=tmp_path,
        timeout_ms=timeout,
        max_output_bytes=4096,
    )


def run(coro):
    return asyncio.run(coro)


def script(tmp_path: Path, code: str) -> Path:
    path = tmp_path / "child.py"
    path.write_text(code, encoding="utf-8")
    return path


def test_missing_isolation_fails_closed_before_spawn(tmp_path: Path):
    source = script(tmp_path, 'print("should never execute")')

    async def scenario():
        with pytest.raises(CapabilityUnavailableError):
            await InteractiveProcessSupervisor(RejectBackend()).open(request(tmp_path, source))

    run(scenario())


def test_bidirectional_pipes_and_no_parent_environment(tmp_path: Path, monkeypatch):
    source = script(
        tmp_path,
        'import os,sys\nfor line in sys.stdin:\n print(line.strip()+"/"+str(os.getenv("POTBOT_SECRET_CANARY")),flush=True)\n',
    )
    monkeypatch.setenv("POTBOT_SECRET_CANARY", "must-not-inherit")

    async def scenario():
        async with await InteractiveProcessSupervisor(LocalOnlyFixtureBackend()).open(
            request(tmp_path, source)
        ) as proc:
            proc.writer.write(b"ping\n")
            await proc.writer.drain()
            assert await asyncio.wait_for(proc.reader.readline(), 1) == b"ping/None\n"
            assert proc.remaining_seconds() > 0
        assert proc.process.returncode == 0
        assert proc.closed

    run(scenario())


def test_stderr_flood_is_drained_not_returned(tmp_path: Path):
    source = script(
        tmp_path,
        'import sys\nsys.stderr.write("X"*200000)\nsys.stderr.flush()\nprint("done",flush=True)\n',
    )

    async def scenario():
        proc = await InteractiveProcessSupervisor(LocalOnlyFixtureBackend()).open(
            request(tmp_path, source)
        )
        assert await asyncio.wait_for(proc.reader.readline(), 2) == b"done\n"
        report = await proc.close()
        assert report.stderr_bytes == 200000
        assert report.stderr_truncated
        assert not hasattr(report, "stderr")
        assert report.processes_stopped

    run(scenario())


def test_watchdog_terminates_idle_child_and_reaps(tmp_path: Path):
    source = script(tmp_path, "import time\ntime.sleep(30)\n")

    async def scenario():
        proc = await InteractiveProcessSupervisor(LocalOnlyFixtureBackend()).open(
            request(tmp_path, source, timeout=100)
        )
        await asyncio.wait_for(proc.process.wait(), 3)
        assert proc.closed
        assert proc.process.returncode is not None

    run(scenario())


def test_closes_after_early_child_exit(tmp_path: Path):
    source = script(tmp_path, "raise SystemExit(13)\n")

    async def scenario():
        proc = await InteractiveProcessSupervisor(LocalOnlyFixtureBackend()).open(
            request(tmp_path, source)
        )
        await proc.process.wait()
        report = await proc.close()
        assert report.return_code == 13
        assert report.processes_stopped

    run(scenario())


def test_authoritative_cancellation_stops_process(tmp_path: Path):
    source = script(tmp_path, "import time\ntime.sleep(30)\n")
    cancelled = [False]

    async def scenario():
        proc = await InteractiveProcessSupervisor(LocalOnlyFixtureBackend()).open(
            request(tmp_path, source, timeout=5000), cancelled=lambda: cancelled[0]
        )
        cancelled[0] = True
        await asyncio.wait_for(proc.process.wait(), 3.0)
        assert proc.closed
        assert proc.process.returncode is not None

    run(scenario())


def test_cancel_check_failure_fails_closed(tmp_path: Path):
    source = script(tmp_path, "import time\ntime.sleep(30)\n")

    def broken():
        raise RuntimeError("database lease unavailable")

    async def scenario():
        proc = await InteractiveProcessSupervisor(LocalOnlyFixtureBackend()).open(
            request(tmp_path, source, timeout=5000), cancelled=broken
        )
        await asyncio.wait_for(proc.process.wait(), 3.0)
        assert proc.closed
        assert proc.process.returncode is not None

    run(scenario())


def test_forced_shutdown_terminates_subprocess_group(tmp_path: Path):
    source = script(
        tmp_path,
        "import subprocess,sys,time\n"
        'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(120)"])\n'
        "print(child.pid,flush=True)\n"
        "time.sleep(120)\n",
    )

    async def scenario():
        proc = await InteractiveProcessSupervisor(LocalOnlyFixtureBackend()).open(
            request(tmp_path, source, timeout=3000)
        )
        child_pid = int((await asyncio.wait_for(proc.reader.readline(), 2)).decode())
        report = await proc.close(force=True)
        assert report.forced_termination
        assert report.processes_stopped
        # A killed grandchild can briefly be a zombie under PID 1 in CI;
        # either disappearance or zombie state shows it no longer executes.
        for _ in range(30):
            status = Path(f"/proc/{child_pid}/status")
            if not status.exists() or "State:\tZ" in status.read_text():
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("grandchild remains runnable after process group shutdown")

    run(scenario())
