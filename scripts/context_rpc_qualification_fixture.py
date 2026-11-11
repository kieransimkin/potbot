"""Build the real context RPC worker used by the disposable qualification jobs."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from potbot.context.service import WorkspaceContextService
from potbot.context.syn_context_route import SynContextRoute
from potbot.tools.agent_homes import AgentHomeRegistry
from potbot.tools.ai_config_history import AiConfigHistoryMonitor
from potbot.tools.execution_contracts import ExecutionPolicy, WorkBinding
from potbot.tools.gui_audit_verification import verify_gui_run
from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor, SandboxedCogOperation
from potbot.tools.shared_workspace_isolation import (
    SharedWorkspaceBubblewrapBackend,
    SharedWorkspaceRuntime,
)
from potbot.tools.supervisor import ProcessSupervisor
from potbot.tools.syn_worker_nodes import SynWorkerNodeMesh
from potbot.tools.worker_telemetry import WorkerMonitorConfig
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger
from scripts.postgres_qualification_fixture import AppEnvironment
from scripts.worker_audit_fixture import monitored_cog_cgroup, prepare_busybox_rootfs

_PROBE = """\
import errno
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, "/opt/potbot")
from context_client import ContextClient, ContextRPCError

def report_probe_failure(error_type, error, trace):
    while trace.tb_next is not None:
        trace = trace.tb_next
    code = error.code if isinstance(error, ContextRPCError) else ""
    Path(".qualification-fault").write_text(
        f"{error_type.__name__} {trace.tb_frame.f_code.co_name}:{trace.tb_lineno} {code}"
    )

sys.excepthook = report_probe_failure
client = ContextClient()
work = client.work_input()
assert work["operation_ref"] == "syn://thing/context-audit-fixture"
assert work["input"]["type_ref"] == "syn://type/in"
model = client.read("agent", "MODEL.md")
assert model["sha256"] == work["input"]["value"]["model_sha256"]
assert Path("MODEL.md").read_text() == model["content"]
try:
    with Path("MODEL.md").open("ab"):
        raise AssertionError("projection unexpectedly writable")
except OSError as error:
    assert error.errno in {errno.EACCES, errno.EROFS}

if "hold" in sys.argv[1:]:
    Path(".qualification-ready").touch()
    next_read = time.monotonic() + 3
    while not Path(".qualification-release").is_file():
        time.sleep(0.05)
        if time.monotonic() >= next_read:
            assert client.read("agent", "MODEL.md")["sha256"] == model["sha256"]
            next_read = time.monotonic() + 3

value = {"invocation_id": work["invocation_id"], "model_revision": model["revision"]}
request_id = work["input"]["value"].get("qualification_request_id") or str(uuid4())
receipt = client.finish_work("syn://type/out", value, request_id=request_id)
assert client.finish_work("syn://type/out", value, request_id=request_id) == receipt
try:
    client.read("agent", "MODEL.md")
except ContextRPCError as error:
    assert error.code == "scope_denied"
else:
    raise AssertionError("final result failed to fence the session")

if "nonzero" in sys.argv[1:]:
    sys.exit(9)
"""


def install_context_guest(rootfs: Path) -> Path:
    """Copy the CI host's distro Python and the shipped stdlib-only RPC client."""
    interpreter = Path("/usr/bin/python3").resolve(strict=True)
    client = Path(__file__).resolve().parents[1] / "sandbox/clients/context_client.py"
    imports = """
import errno, pathlib, sys, sysconfig
sys.path.insert(0, sys.argv[1])
import context_client
print(sysconfig.get_path("stdlib"))
for path in sorted({getattr(module, "__file__", "") or "" for module in sys.modules.values()}):
    if path.endswith(".so"):
        print(path)
"""
    stdlib, *extensions = subprocess.check_output(
        [interpreter, "-I", "-S", "-c", imports, client.parent], text=True, timeout=10
    ).splitlines()

    def copy_binary(source: Path) -> None:
        target = rootfs / source.relative_to("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    copy_binary(interpreter)
    shutil.copytree(
        stdlib,
        rootfs / Path(stdlib).relative_to("/"),
        ignore=shutil.ignore_patterns("__pycache__", "site-packages", "test", "tests"),
    )
    links = subprocess.check_output(["ldd", interpreter, *extensions], text=True, timeout=15)
    libraries = set()
    for line in links.splitlines():
        candidate = line.partition("=>")[2].strip() if "=>" in line else line.strip()
        if candidate.startswith("/") and not candidate.endswith(":"):
            libraries.add(Path(candidate.split()[0]))
    for library in sorted(libraries):
        copy_binary(library)
    shutil.copy2(client, rootfs / "opt/potbot/context_client.py")
    (rootfs / "opt/potbot/context-probe.py").write_text(_PROBE, encoding="utf-8")
    entrypoint = rootfs / "bin/context-probe"
    entrypoint.write_text(
        f'#!/bin/sh\nexec {interpreter} -I -S -B /opt/potbot/context-probe.py "$@"\n',
        encoding="utf-8",
    )
    entrypoint.chmod(0o555)
    return entrypoint


@dataclass(frozen=True)
class ContextWorkerFixture:
    environment: AppEnvironment
    executor: SandboxedCogExecutor
    operation: SandboxedCogOperation
    directories: dict[UUID, Path]
    audit: Path
    cgroup_root: Path

    @property
    def workspace_audit(self) -> Path:
        return self.audit / str(self.environment.context.workspace_id)

    def model(self, agent: UUID) -> dict[str, Any]:
        return WorkspaceContextService(self.environment.db).read(
            self.environment.context, "agent", str(agent), "MODEL.md"
        )

    def verify_result(
        self, agent: UUID, run: UUID, expected: dict[str, Any], *, exit_code: int
    ) -> int:
        actor = self.environment.context
        receipt = verify_gui_run(
            self.audit, actor.workspace_id, run, identity_kind="agent", subject_id=agent
        )
        count = receipt["tracedProcesses"]
        assert receipt["qualified"] is True and type(count) is int and count >= 2
        (tree,) = (
            path.with_name("process-tree.jsonl")
            for path in self.workspace_audit.glob("*/identity.json")
            if json.loads(path.read_text())["run_id"] == str(run)
        )
        exits = [json.loads(line)["exit"] for line in tree.read_text().splitlines()]
        assert {"state": "exited", "code": exit_code} in exits
        assert not any(path.is_dir() for path in self.cgroup_root.iterdir())
        assert not list((self.workspace_audit / ".active").iterdir())
        with self.environment.db.transaction(actor) as conn:
            record = conn.execute(
                "SELECT s.agent_id,s.state,w.result_json::jsonb AS result "
                "FROM workspace_context_sessions s JOIN workspace_context_work_results w "
                "ON s.workspace_id=w.workspace_id AND s.id=w.session_id "
                "AND s.result_request_id=w.request_id WHERE s.run_id=%s",
                (run,),
            ).fetchone()
        assert record is not None
        assert record["agent_id"] == agent and record["state"] == "revoked"
        assert record["result"] == {
            "schema_version": 1,
            "type_ref": "syn://type/out",
            "value": expected,
        }
        return count


@contextmanager
def context_worker_fixture(
    base: Path,
    app_env: AppEnvironment,
    agents: tuple[UUID, ...],
    mesh: SynWorkerNodeMesh,
    *,
    development_syn_fixture: bool = False,
    syn_context_route: SynContextRoute | None = None,
    max_wall_ms: int = 15000,
    build_commit: str = "c" * 40,
    monitor: Callable[
        [Path, Path], AbstractContextManager[WorkerMonitorConfig]
    ] = monitored_cog_cgroup,
) -> Iterator[ContextWorkerFixture]:
    rootfs = base / "image"
    prepare_busybox_rootfs(rootfs)
    for name in ("etc/ai", "etc/ai-past", "usr/local/skills"):
        (rootfs / name).mkdir(parents=True)
    for role in ("AGENTS.md", "MODEL.md", "MEMORY.md"):
        (rootfs / role).touch()
    entrypoint = install_context_guest(rootfs)
    worker_sha = hashlib.sha256(entrypoint.read_bytes()).hexdigest()
    home, staging, models, tools, private, audit, ai, past = (
        base / name
        for name in ("home", "staging", "models", "tools", "private", "audit", "ai", "past")
    )
    for path in (home / ".tmp", staging, models, tools, private, audit, ai, past):
        path.mkdir(mode=0o700 if path in (private, audit) else 0o755, parents=True)
    ai.chmod(0o775)
    runtime = SharedWorkspaceRuntime(
        worker_sha,
        rootfs,
        home,
        "b" * 64,
        model_staging=staging,
        shared_models=models,
        shared_tools=tools,
        ai_config_dir=ai,
        ai_history_dir=past,
    )
    actor = app_env.context
    workspace = actor.workspace_id
    ledger = WorkspaceLeaseLedger(private / "leases.sqlite", workspace_home=home)
    ledger.pin_image(
        workspace,
        digest=runtime.image_digest,
        image_ref=f"ghcr.io/test/image@sha256:{runtime.image_digest}",
        build_commit=build_commit,
        platform="linux/amd64",
        rootfs_path=rootfs,
        qualification_sha256=runtime.qualification_sha256,
    )
    homes = AgentHomeRegistry(ledger, workspace_name="context-audit")
    directories = {agent: home / homes.provision(workspace, agent).directory for agent in agents}
    service = WorkspaceContextService(app_env.db)
    for agent in agents:
        service.provision_private(actor, "agent", str(agent), idempotency_key=uuid4().hex)
    operation = SandboxedCogOperation(
        WorkBinding(
            "tool",
            "syn://thing/context-audit-fixture",
            "syn://type/in",
            "syn://type/out",
            worker_sha,
            "read_only",
            ExecutionPolicy("d" * 64, "e" * 64, "f" * 64, "test", max_wall_ms=max_wall_ms),
        ),
        ("/bin/context-probe",),
        worker_sha,
        protocol="context-rpc/v1",
    )
    with (
        monitor(private, audit) as config,
        AiConfigHistoryMonitor(config_dir=ai, past_dir=past) as ai_monitor,
    ):
        executor = SandboxedCogExecutor(
            runtime,
            ledger,
            homes,
            ProcessSupervisor(
                SharedWorkspaceBubblewrapBackend(runtime, ai_monitor=ai_monitor),
                monitor_config=config,
            ),
            mesh,
            development_syn_fixture=development_syn_fixture,
            syn_context_route=syn_context_route,
        )
        executor.bind_context(app_env.db, actor, (operation,))
        yield ContextWorkerFixture(
            app_env, executor, operation, directories, audit, config.cgroup_root
        )
