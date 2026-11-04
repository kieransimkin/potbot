"""Live Linux checks for the U03 worker audit completion boundary."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.tools.worker_audit import process_tree
from potbot.tools.worker_telemetry import (
    _STRACE_FLAGS,
    _STRACE_PID_NAMESPACE,
    _cgroup_populated,
    _terminate_cgroup,
)

_WORKLOAD = """
import asyncio
import errno
import hashlib
import json
import os
import re
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


@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("POTBOT_TEST_WORKER_CGROUP") != "1",
    reason="requires a disposable Linux namespace fixture with PID translation",
)
def test_real_pid_namespace_joins_forks_to_tracer_ids(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "strace",
            *_STRACE_FLAGS,
            _STRACE_PID_NAMESPACE,
            "-o",
            str(tmp_path / "trace"),
            "--",
            "unshare",
            "--fork",
            "--pid",
            "--mount-proc",
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
    assert tree["namespace_translated_edges"] >= 3
    assert len(tree["nodes"]) == len(traces)
    assert any(node["exit"]["state"] == "signalled" for node in tree["nodes"])

    translated = next(path for path in traces if "in strace's PID NS" in path.read_text())
    translated.write_text(re.sub(r" /\* \d+ in strace's PID NS \*/", "", translated.read_text()))
    incomplete = process_tree(traces)
    assert incomplete["tree_complete"] is False


_COG_PROBE = """#!/bin/sh
set -eu
IFS= read -r request || test -n "$request"
/bin/busybox true &
wait "$!"
printf '%s\\n' '{"schema_version":1,"type_ref":"syn://type/out","value":{"done":true}}'
"""


@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("POTBOT_TEST_WORKER_CGROUP") != "1",
    reason="requires a disposable Linux fixture with real worker accounting",
)
def test_registered_cogs_join_bubblewrap_traces_and_taskstats(tmp_path: Path) -> None:
    from potbot.cogs.contracts import CogRunContext, TypedCogValue
    from potbot.domain.auth import WorkspaceContext
    from potbot.domain.errors import CapabilityUnavailableError
    from potbot.tools.agent_homes import AgentHomeRegistry
    from potbot.tools.execution_contracts import ExecutionPolicy, WorkBinding
    from potbot.tools.gui_audit_verification import verify_gui_run
    from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor, SandboxedCogOperation
    from potbot.tools.shared_workspace_isolation import (
        SharedWorkspaceBubblewrapBackend,
        SharedWorkspaceRuntime,
    )
    from potbot.tools.supervisor import ProcessSupervisor
    from potbot.tools.syn_worker_nodes import SynWorkerNodeMesh
    from potbot.tools.taskstats_exit import TaskstatsExitStream
    from potbot.tools.worker_telemetry import WorkerMonitorConfig
    from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger

    stream = TaskstatsExitStream()
    try:
        stream.open()
    except CapabilityUnavailableError as error:
        cause = error.__cause__
        if isinstance(cause, OSError) and cause.errno in {
            errno.ENOENT,
            errno.EPERM,
            errno.EOPNOTSUPP,
        }:
            pytest.skip(f"runner cannot register TASKSTATS (errno {cause.errno})")
        raise
    finally:
        stream.close()

    rootfs, home, staging, models, tools, private, audit = (
        tmp_path / name
        for name in ("image", "home", "staging", "models", "tools", "private", "audit")
    )
    for path in (
        home / ".tmp",
        staging,
        models,
        tools,
        private,
        audit,
        *(
            rootfs / name
            for name in (
                "home",
                "proc",
                "dev",
                "bin",
                "var/ai",
                "var/logs/ai",
                "opt/potbot/models",
                "usr/local/tools",
            )
        ),
    ):
        path.mkdir(mode=0o700 if path == audit else 0o755, parents=True)
    (rootfs / "tmp").symlink_to("/home/.tmp")
    shutil.copy2("/usr/bin/busybox", rootfs / "bin/busybox")
    (rootfs / "bin/sh").symlink_to("busybox")
    worker = rootfs / "bin/qualification-worker"
    worker.write_text(_COG_PROBE)
    worker.chmod(0o555)
    worker_sha = hashlib.sha256(worker.read_bytes()).hexdigest()

    runtime = SharedWorkspaceRuntime(
        worker_sha,
        rootfs,
        home,
        "b" * 64,
        model_staging=staging,
        shared_models=models,
        shared_tools=tools,
    )
    workspace, principal = uuid4(), uuid4()
    ledger = WorkspaceLeaseLedger(private / "leases.sqlite", workspace_home=home)
    ledger.pin_image(
        workspace,
        digest=runtime.image_digest,
        image_ref=f"ghcr.io/test/image@sha256:{runtime.image_digest}",
        build_commit="c" * 40,
        platform="linux/amd64",
        rootfs_path=rootfs,
        qualification_sha256=runtime.qualification_sha256,
    )
    homes = AgentHomeRegistry(ledger, workspace_name="audit-test")
    agents = (uuid4(), uuid4())
    for agent in agents:
        homes.provision(workspace, agent)
    helper = private / "cgroup-join"
    source = Path(__file__).resolve().parents[2] / "native/potbot-cgroup-join/cgroup_join.c"
    subprocess.run(["cc", "-O2", "-o", str(helper), str(source)], check=True, timeout=15)
    tracer = Path("/usr/bin/strace").resolve()
    cgroup = Path("/sys/fs/cgroup") / f"potbot-u03-cog-test-{uuid4().hex}"
    cgroup.mkdir()
    try:
        (cgroup / "cgroup.subtree_control").write_text("+cpu +memory +io +pids")
        config = WorkerMonitorConfig(
            audit,
            cgroup,
            tracer,
            helper,
            hashlib.sha256(tracer.read_bytes()).hexdigest(),
            hashlib.sha256(helper.read_bytes()).hexdigest(),
        )
        executor = SandboxedCogExecutor(
            runtime,
            ledger,
            homes,
            ProcessSupervisor(SharedWorkspaceBubblewrapBackend(runtime), monitor_config=config),
            SynWorkerNodeMesh(),
            development_syn_fixture=True,
        )
        op = SandboxedCogOperation(
            WorkBinding(
                "tool",
                "syn://thing/audit-fixture",
                "syn://type/in",
                "syn://type/out",
                worker_sha,
                "read_only",
                ExecutionPolicy("d" * 64, "e" * 64, "f" * 64, "test", max_wall_ms=15000),
            ),
            ("/bin/qualification-worker",),
            worker_sha,
        )
        actor = WorkspaceContext(workspace, principal)
        for agent in agents:
            run = uuid4()
            try:
                output = asyncio.run(
                    executor.execute(
                        op,
                        TypedCogValue("syn://type/in", {"probe": True}),
                        CogRunContext("a" * 64, uuid4(), uuid4(), run),
                        actor,
                        agent,
                    )
                )
            except Exception:
                # The fixture is synthetic. Report bounded reasons and counts only.
                for path in sorted((audit / str(workspace)).glob("*/lifetime.json")):
                    evidence = json.loads(path.read_text())
                    print(
                        {
                            key: evidence.get(key)
                            for key in (
                                "audit_failure",
                                "trace_integrity",
                                "traced_pid_count",
                                "per_pid_kernel_exit_count",
                            )
                        }
                    )
                for path in sorted((audit / str(workspace)).glob("*/live.json")):
                    status = json.loads(path.read_text())
                    print({"state": status.get("state"), "fault": status.get("fault")})
                raise
            assert output.value == {"done": True}
            receipt = verify_gui_run(audit, workspace, run, identity_kind="agent", subject_id=agent)
            assert receipt["qualified"] is True and receipt["tracedProcesses"] >= 3
        assert not any(path.is_dir() for path in cgroup.iterdir())
    finally:
        for path in cgroup.iterdir():
            if path.is_dir():
                assert _terminate_cgroup(path)
                path.rmdir()
        cgroup.rmdir()
