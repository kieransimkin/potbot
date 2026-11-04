"""Disposable Linux worker-audit setup used by the integration fixtures."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from potbot.tools.taskstats_exit import TaskstatsExitStream
from potbot.tools.worker_telemetry import WorkerMonitorConfig, _terminate_cgroup


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
