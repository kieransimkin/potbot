"""Disposable Linux worker-audit setup used by the integration fixtures."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from potbot.cogs.contracts import TypedCogValue
from potbot.tools.syn_worker_nodes import SynWorkerNode
from potbot.tools.taskstats_exit import TaskstatsExitStream
from potbot.tools.worker_telemetry import WorkerMonitorConfig, _cgroup_populated, _terminate_cgroup


def prepare_busybox_rootfs(rootfs: Path) -> None:
    for name in (
        "home",
        "proc",
        "dev",
        "bin",
        "var/ai",
        "var/logs/ai",
        "opt/potbot/models",
        "usr/local/tools",
    ):
        (rootfs / name).mkdir(parents=True)
    (rootfs / "tmp").symlink_to("/home/.tmp")
    shutil.copy2("/usr/bin/busybox", rootfs / "bin/busybox")
    (rootfs / "bin/sh").symlink_to("busybox")


@contextmanager
def monitored_cog_cgroup(private: Path, audit: Path) -> Iterator[WorkerMonitorConfig]:
    stream = TaskstatsExitStream()
    try:
        stream.open()
    finally:
        stream.close()

    helper = private / "cgroup-join"
    helper_source = Path(__file__).resolve().parents[1] / "native/potbot-cgroup-join/cgroup_join.c"
    subprocess.run(["cc", "-O2", "-o", str(helper), str(helper_source)], check=True, timeout=15)
    tracer = Path("/usr/bin/strace").resolve()
    cgroup = Path("/sys/fs/cgroup") / f"potbot-u03-cog-test-{uuid4().hex}"
    cgroup.mkdir()
    try:
        (cgroup / "cgroup.subtree_control").write_text("+cpu +memory +io +pids")
        yield WorkerMonitorConfig(
            audit,
            cgroup,
            tracer,
            helper,
            hashlib.sha256(tracer.read_bytes()).hexdigest(),
            hashlib.sha256(helper.read_bytes()).hexdigest(),
        )
    finally:
        for path in cgroup.iterdir():
            if path.is_dir():
                assert _terminate_cgroup(path)
                path.rmdir()
        cgroup.rmdir()


async def require_running(task: asyncio.Task[TypedCogValue]) -> None:
    if task.done():
        await task
        raise AssertionError("Worker exited before native observation")


def observed_sequence(peer: SynWorkerNode, agent: UUID, run: UUID) -> int:
    peers = peer.snapshot()["peers"]
    if isinstance(peers, list):
        for beat in peers:
            if beat["agent_id"] == str(agent) and beat["worker_run_id"] == str(run):
                return int(beat["last_sequence"])
    return 0


async def release_after_native_heartbeat(
    task: asyncio.Task[TypedCogValue],
    peer: SynWorkerNode,
    directory: Path,
    workspace_audit: Path,
    cgroup_root: Path,
    agent: UUID,
    run: UUID,
) -> None:
    """Release a synthetic worker after a new signed peer observation."""
    try:
        async with asyncio.timeout(45):
            while True:
                await require_running(task)
                entries = list((workspace_audit / ".active").glob("*.json"))
                if entries and (directory / ".qualification-ready").is_file():
                    break
                await asyncio.sleep(0.05)
    except TimeoutError as exc:
        raise AssertionError("Authorized Cog did not reach its live cgroup") from exc

    (active,) = entries
    leaf = cgroup_root / UUID(active.stem).hex
    assert _cgroup_populated(leaf)
    baseline = observed_sequence(peer, agent, run)
    try:
        async with asyncio.timeout(24):
            while observed_sequence(peer, agent, run) <= max(baseline, 1):
                await require_running(task)
                assert peer.healthy, "Native peer stopped during live Cog observation"
                await asyncio.sleep(0.05)
    except TimeoutError as exc:
        raise AssertionError("Native peer missed a new heartbeat from the live Cog") from exc
    assert _cgroup_populated(leaf)
    (directory / ".qualification-release").touch()
