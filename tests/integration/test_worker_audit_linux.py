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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from potbot.tools.worker_audit import process_tree
from potbot.tools.worker_telemetry import (
    _STRACE_FLAGS,
    _STRACE_PID_NAMESPACE,
    WorkerMonitorConfig,
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
    from potbot.domain.errors import CapabilityUnavailableError
    from potbot.tools.taskstats_exit import TaskstatsExitStream

    stream = TaskstatsExitStream()
    try:
        stream.open()
    except CapabilityUnavailableError as error:
        cause = error.__cause__
        if (
            isinstance(cause, OSError)
            and cause.errno in {errno.ENOENT, errno.EPERM, errno.EOPNOTSUPP}
            and os.environ.get("POTBOT_REQUIRE_WORKER_TASKSTATS") != "1"
        ):
            pytest.skip(f"runner cannot register TASKSTATS (errno {cause.errno})")
        raise
    finally:
        stream.close()

    helper = private / "cgroup-join"
    helper_source = Path(__file__).resolve().parents[2] / "native/potbot-cgroup-join/cgroup_join.c"
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


_COG_PROBE = """#!/bin/sh
set -eu
IFS= read -r request || test -n "$request"
if [ "${1-}" = hold ] || [ "${1-}" = release ]; then
    /bin/busybox setsid /bin/sh -c '
        printf "%s\\n" "$$" > .qualification-ready
        if [ "$1" = hold ]; then
            exec /bin/busybox sleep 60
        fi
        until [ -f .qualification-release ]; do /bin/busybox sleep 0.25; done
    ' probe "$1" &
    wait "$!"
else
    /bin/busybox true &
    wait "$!"
fi
printf '%s\\n' '{"schema_version":1,"type_ref":"syn://type/out","value":{"done":true}}'
"""


@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("POTBOT_TEST_WORKER_CGROUP") != "1",
    reason="requires a disposable Linux fixture with real worker accounting",
)
@pytest.mark.parametrize("scenario", ["complete", "cancel", "timeout-overlap", "overlap-renewal"])
def test_registered_cogs_join_bubblewrap_traces_and_taskstats(
    tmp_path: Path, scenario: str
) -> None:
    from potbot.cogs.contracts import CogRunContext, TypedCogValue
    from potbot.domain.auth import WorkspaceContext
    from potbot.domain.errors import (
        IndeterminateExecutionError,
        StateMismatchError,
    )
    from potbot.tools.agent_homes import AgentHome, AgentHomeRegistry
    from potbot.tools.execution_contracts import ExecutionPolicy, WorkBinding
    from potbot.tools.gui_audit_verification import verify_gui_run
    from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor, SandboxedCogOperation
    from potbot.tools.secrets import CaptureRedactor
    from potbot.tools.shared_workspace_isolation import (
        SharedWorkspaceBubblewrapBackend,
        SharedWorkspaceRuntime,
    )
    from potbot.tools.supervisor import ProcessRequest, ProcessResult, ProcessSupervisor
    from potbot.tools.syn_worker_nodes import SynWorkerNodeMesh
    from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger
    from potbot.tools.workspace_supervision import WorkspaceExecutorIdentity, WorkspaceLeaseGuard

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
    ):
        path.mkdir(mode=0o700 if path == audit else 0o755, parents=True)
    prepare_busybox_rootfs(rootfs)
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
    with monitored_cog_cgroup(private, audit) as config:
        cgroup = config.cgroup_root
        results: dict[UUID, ProcessResult] = {}

        class ObservedSupervisor(ProcessSupervisor):
            def run(
                self,
                request: ProcessRequest,
                *,
                cancelled: Callable[[], bool] | None = None,
                redactor: CaptureRedactor | None = None,
            ) -> ProcessResult:
                result = super().run(request, cancelled=cancelled, redactor=redactor)
                assert request.telemetry_run_id is not None
                results[request.telemetry_run_id] = result
                return result

        def register_workspace(
            worker_runtime: SharedWorkspaceRuntime, name: str, agent_count: int
        ) -> tuple[WorkspaceContext, WorkspaceLeaseLedger, SandboxedCogExecutor, list[AgentHome]]:
            context = WorkspaceContext(uuid4(), uuid4())
            access = WorkspaceLeaseLedger(
                private / f"{name}.sqlite", workspace_home=worker_runtime.home
            )
            access.pin_image(
                context.workspace_id,
                digest=worker_runtime.image_digest,
                image_ref=f"ghcr.io/test/image@sha256:{worker_runtime.image_digest}",
                build_commit="c" * 40,
                platform="linux/amd64",
                rootfs_path=rootfs,
                qualification_sha256=worker_runtime.qualification_sha256,
            )
            homes = AgentHomeRegistry(access, workspace_name=name)
            registered = [
                homes.provision(context.workspace_id, uuid4()) for _ in range(agent_count)
            ]
            worker_executor = SandboxedCogExecutor(
                worker_runtime,
                access,
                homes,
                ObservedSupervisor(
                    SharedWorkspaceBubblewrapBackend(worker_runtime), monitor_config=config
                ),
                SynWorkerNodeMesh(),
                development_syn_fixture=True,
            )
            return context, access, worker_executor, registered

        actor, ledger, executor, registered = register_workspace(runtime, "audit-test", 2)
        workspace = actor.workspace_id
        agent_homes = {entry.agent_id: entry for entry in registered}
        agents = tuple(agent_homes)
        source = TypedCogValue("syn://type/in", {"probe": True})
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

        async def wait_for_detached(
            directory: Path, workspace_id: UUID, pending: asyncio.Task[TypedCogValue]
        ) -> tuple[Path, int]:
            marker = directory / ".qualification-ready"
            active = audit / str(workspace_id) / ".active"
            async with asyncio.timeout(8):
                while True:
                    if pending.done():
                        await pending
                        pytest.fail("Cog completed without a detached child")
                    markers = list(active.glob("*.json"))
                    if marker.exists() and marker.read_text().strip() and markers:
                        break
                    await asyncio.sleep(0.025)
            (entry,) = markers
            leaf = cgroup / UUID(entry.stem).hex
            guest_pid = int(marker.read_text())
            detached = []
            for pid in map(int, (leaf / "cgroup.procs").read_text().split()):
                try:
                    status = Path(f"/proc/{pid}/status").read_text()
                    namespace_line = re.search(r"^NSpid:[ \t]+(.+)$", status, re.MULTILINE)
                    namespace_ids = namespace_line[1].split() if namespace_line else []
                    if (
                        len(namespace_ids) >= 2
                        and int(namespace_ids[-1]) == guest_pid
                        and os.getsid(pid) == pid
                    ):
                        detached.append(pid)
                except (FileNotFoundError, ProcessLookupError):
                    continue
            assert len(detached) == 1, "detached guest child must be alive in the worker cgroup"
            return leaf, detached[0]

        async def cancel_after_detach(agent: UUID, run: UUID) -> None:
            context = CogRunContext("a" * 64, uuid4(), uuid4(), run)
            cancel_op = replace(op, argv=(*op.argv, "hold"))
            pending = asyncio.create_task(
                executor.execute(cancel_op, source, context, actor, agent)
            )
            try:
                leaf, host_pid = await wait_for_detached(
                    home / agent_homes[agent].directory, workspace, pending
                )
                assert pending.cancel()
                with pytest.raises(IndeterminateExecutionError, match="Cog task cancelled"):
                    await pending
                assert not leaf.exists(), "cancelled Cog left its cgroup behind"
                assert not list((audit / str(workspace) / ".active").iterdir())
                # The shared /home lease fences both Agents until host reconciliation.
                with pytest.raises(StateMismatchError, match="conflicting or indeterminate"):
                    await executor.execute(
                        op, source, replace(context, invocation_id=uuid4()), actor, agents[1]
                    )
                guard = WorkspaceLeaseGuard.exclusive_home(
                    ledger,
                    WorkspaceExecutorIdentity(workspace, agent, runtime.image_digest),
                    operation="cog.sandbox.execute",
                    idempotency_key="cog-" + run.hex,
                )
                lease = ledger.claim(guard.request)
                assert lease.state == "indeterminate"
                # The disposable probe's only persistent write was its marker.
                evidence = hashlib.sha256(
                    f"{run.hex}:{leaf.name}:released:{host_pid}".encode()
                ).hexdigest()
                ledger.reconcile(lease, evidence_sha256=evidence)
                with pytest.raises(
                    StateMismatchError, match="workspace invocation already recorded"
                ):
                    await executor.execute(cancel_op, source, context, actor, agent)
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

        def active_lease(access: WorkspaceLeaseLedger, workspace_id: UUID) -> tuple[int, int]:
            (row,) = access.active(workspace_id)
            assert row["state"] == "active"
            generation, expires = row["generation"], row["expires_ns"]
            assert type(generation) is int and type(expires) is int
            return generation, expires

        def verify_stopped_worker(
            leaf: Path,
            access: WorkspaceLeaseLedger,
            scope: WorkspaceContext,
            run: CogRunContext,
            agent: UUID,
            child_pid: int,
        ) -> set[str]:
            workspace_audit = audit / str(scope.workspace_id)
            assert not leaf.exists()
            assert not list((workspace_audit / ".active").iterdir())
            assert not access.active(scope.workspace_id)
            receipt = verify_gui_run(
                audit,
                scope.workspace_id,
                run.invocation_id,
                identity_kind="agent",
                subject_id=agent,
            )
            assert receipt["qualified"] is True
            traces = {
                path.name for path in (workspace_audit / str(UUID(hex=leaf.name))).glob("trace.*")
            }
            assert f"trace.{child_pid}" in traces
            return traces

        async def overlap_with_peer(*, time_out: bool) -> None:
            peer_home = tmp_path / "peer-home"
            peer_staging = tmp_path / "peer-staging"
            (peer_home / ".tmp").mkdir(parents=True)
            peer_staging.mkdir()
            peer_runtime = replace(runtime, home=peer_home, model_staging=peer_staging)
            peer_actor, peer_ledger, peer_executor, (peer, peer_other) = register_workspace(
                peer_runtime, "audit-peer", 2
            )
            primary_run = CogRunContext("a" * 64, uuid4(), uuid4(), uuid4())
            peer_run = replace(primary_run, invocation_id=uuid4())
            timeout_ms = 8000
            waiting = replace(
                op,
                binding=replace(op.binding, policy=replace(op.binding.policy, max_wall_ms=35000)),
                argv=(*op.argv, "release"),
            )
            primary_op = (
                replace(
                    waiting,
                    binding=replace(
                        op.binding, policy=replace(op.binding.policy, max_wall_ms=timeout_ms)
                    ),
                    argv=(*op.argv, "hold"),
                )
                if time_out
                else waiting
            )
            primary_dir = home / agent_homes[agents[0]].directory
            peer_dir = peer_home / peer.directory
            primary_task = asyncio.create_task(
                executor.execute(primary_op, source, primary_run, actor, agents[0])
            )
            peer_task = asyncio.create_task(
                peer_executor.execute(waiting, source, peer_run, peer_actor, peer.agent_id)
            )
            jobs = (primary_task, peer_task)
            try:
                (primary_leaf, primary_pid), (peer_leaf, peer_pid) = await asyncio.gather(
                    wait_for_detached(primary_dir, workspace, primary_task),
                    wait_for_detached(peer_dir, peer_actor.workspace_id, peer_task),
                )
                assert primary_leaf != peer_leaf and primary_pid != peer_pid
                assert not primary_task.done() and not peer_task.done()

                if time_out:
                    with pytest.raises((StateMismatchError, IndeterminateExecutionError)):
                        await asyncio.wait_for(asyncio.shield(primary_task), timeout=14)
                    expired = results[primary_run.invocation_id]
                    assert expired.dispatched and expired.elapsed_ms >= timeout_ms
                    assert expired.error_code in {"timeout", "audit_incomplete"}
                    assert not primary_leaf.exists()
                    assert not list((audit / str(workspace) / ".active").iterdir())
                    (retained,) = ledger.active(workspace)
                    assert retained["state"] == "indeterminate"
                    primary_traces = {f"trace.{primary_pid}"}
                else:
                    generation, expires = active_lease(ledger, workspace)
                    peer_generation, peer_expires = active_lease(
                        peer_ledger, peer_actor.workspace_id
                    )
                    for worker, scope, contender in (
                        (executor, actor, agents[1]),
                        (peer_executor, peer_actor, peer_other.agent_id),
                    ):
                        blocked = replace(primary_run, invocation_id=uuid4())
                        with pytest.raises(
                            StateMismatchError, match="conflicting or indeterminate"
                        ):
                            await worker.execute(op, source, blocked, scope, contender)
                        assert blocked.invocation_id not in results

                    # The real guard renews every ten seconds. Keep both children live
                    # until the controller observes durable renewal in both ledgers.
                    async with asyncio.timeout(18):
                        while True:
                            assert not primary_task.done() and not peer_task.done()
                            primary_now = active_lease(ledger, workspace)
                            peer_now = active_lease(peer_ledger, peer_actor.workspace_id)
                            assert primary_now[0] == generation and peer_now[0] == peer_generation
                            if primary_now[1] > expires and peer_now[1] > peer_expires:
                                break
                            await asyncio.sleep(0.1)
                    assert _cgroup_populated(primary_leaf) and _cgroup_populated(peer_leaf)
                    (primary_dir / ".qualification-release").touch()
                    assert (await asyncio.wait_for(primary_task, timeout=8)).value == {"done": True}
                    primary_traces = verify_stopped_worker(
                        primary_leaf, ledger, actor, primary_run, agents[0], primary_pid
                    )

                assert not peer_task.done() and _cgroup_populated(peer_leaf)
                (peer_dir / ".qualification-release").touch()
                assert (await asyncio.wait_for(peer_task, timeout=8)).value == {"done": True}
                peer_traces = verify_stopped_worker(
                    peer_leaf, peer_ledger, peer_actor, peer_run, peer.agent_id, peer_pid
                )
                assert primary_traces.isdisjoint(peer_traces)
            finally:
                for job in jobs:
                    if not job.done():
                        job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)

        try:
            overlap = scenario in {"timeout-overlap", "overlap-renewal"}
            if overlap:
                asyncio.run(overlap_with_peer(time_out=scenario == "timeout-overlap"))
            for agent in () if overlap else agents:
                run = uuid4()
                if scenario == "cancel" and agent == agents[0]:
                    asyncio.run(cancel_after_detach(agent, run))
                    continue
                output = asyncio.run(
                    executor.execute(
                        op,
                        source,
                        CogRunContext("a" * 64, uuid4(), uuid4(), run),
                        actor,
                        agent,
                    )
                )
                assert output.value == {"done": True}
                receipt = verify_gui_run(
                    audit, workspace, run, identity_kind="agent", subject_id=agent
                )
                assert receipt["qualified"] is True and receipt["tracedProcesses"] >= 3
        except Exception:
            # The fixture is synthetic. Report bounded reasons and counts only.
            for path in sorted(audit.glob("*/*/lifetime.json")):
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
            for path in sorted(audit.glob("*/*/live.json")):
                status = json.loads(path.read_text())
                print({"state": status.get("state"), "fault": status.get("fault")})
            raise
        assert not any(path.is_dir() for path in cgroup.iterdir())
