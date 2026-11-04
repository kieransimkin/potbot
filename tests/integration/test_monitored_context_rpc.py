"""Qualify the installed context RPC profile with live Linux audit and PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.context.service import WorkspaceContextService
from potbot.domain.errors import StateMismatchError
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
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger
from tests.integration.conftest import AppEnvironment
from tests.integration.test_worker_audit_linux import monitored_cog_cgroup, prepare_busybox_rootfs

_PROBE = """\
import errno
import sys
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

value = {"invocation_id": work["invocation_id"], "model_revision": model["revision"]}
request_id = str(uuid4())
receipt = client.finish_work("syn://type/out", value, request_id=request_id)
assert client.finish_work("syn://type/out", value, request_id=request_id) == receipt
try:
    client.read("agent", "MODEL.md")
except ContextRPCError as error:
    assert error.code == "scope_denied"
else:
    raise AssertionError("final result failed to fence the session")

if sys.argv[1:] == ["nonzero"]:
    sys.exit(9)
"""


def install_context_guest(rootfs: Path) -> Path:
    """Copy the CI host's distro Python and the shipped stdlib-only RPC client."""
    interpreter = Path("/usr/bin/python3").resolve(strict=True)
    client = Path(__file__).resolve().parents[2] / "sandbox/clients/context_client.py"
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


@pytest.mark.postgres
@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("POTBOT_TEST_WORKER_CGROUP") != "1",
    reason="requires disposable Linux cgroup, TASKSTATS and PostgreSQL",
)
def test_registered_context_rpc_result_requires_audited_worker_exit(
    tmp_path: Path, app_env: AppEnvironment
) -> None:
    rootfs = tmp_path / "image"
    prepare_busybox_rootfs(rootfs)
    for name in ("etc/ai", "etc/ai-past", "usr/local/skills"):
        (rootfs / name).mkdir(parents=True)
    for role in ("AGENTS.md", "MODEL.md", "MEMORY.md"):
        (rootfs / role).touch()
    entrypoint = install_context_guest(rootfs)
    worker_sha = hashlib.sha256(entrypoint.read_bytes()).hexdigest()

    home, staging, models, tools, private, audit, ai, past = (
        tmp_path / name
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
        build_commit="c" * 40,
        platform="linux/amd64",
        rootfs_path=rootfs,
        qualification_sha256=runtime.qualification_sha256,
    )
    homes = AgentHomeRegistry(ledger, workspace_name="context-audit")
    registered = homes.provision(workspace, uuid4())
    agent = registered.agent_id
    diagnostic = home / registered.directory / ".qualification-fault"
    service = WorkspaceContextService(app_env.db)
    service.provision_private(actor, "agent", str(agent), idempotency_key=uuid4().hex)
    model = service.read(actor, "agent", str(agent), "MODEL.md")
    op = SandboxedCogOperation(
        WorkBinding(
            "tool",
            "syn://thing/context-audit-fixture",
            "syn://type/in",
            "syn://type/out",
            worker_sha,
            "read_only",
            ExecutionPolicy("d" * 64, "e" * 64, "f" * 64, "test", max_wall_ms=15000),
        ),
        ("/bin/context-probe",),
        worker_sha,
        protocol="context-rpc/v1",
    )
    value = TypedCogValue("syn://type/in", {"model_sha256": model["sha256"]})

    with (
        monitored_cog_cgroup(private, audit) as config,
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
            SynWorkerNodeMesh(),
            development_syn_fixture=True,
        )
        executor.bind_context(app_env.db, actor, (op,))
        for clean_exit in (True, False):
            run = uuid4()
            context = CogRunContext("a" * 64, uuid4(), uuid4(), run)
            expected = {"invocation_id": str(run), "model_revision": model["revision"]}
            if clean_exit:
                try:
                    output = asyncio.run(executor.execute(op, value, context, actor, agent))
                except StateMismatchError as error:
                    if diagnostic.is_file():
                        error.add_note(diagnostic.read_text()[:160])
                    raise
                assert output.type_ref == "syn://type/out" and output.value == expected
                assert not ledger.active(workspace)
            else:
                failing = replace(op, argv=(*op.argv, "nonzero"))
                with pytest.raises(
                    StateMismatchError, match="did not complete its declared result"
                ):
                    asyncio.run(executor.execute(failing, value, context, actor, agent))
                (retained,) = ledger.active(workspace)
                assert retained["state"] == "indeterminate"

            receipt = verify_gui_run(audit, workspace, run, identity_kind="agent", subject_id=agent)
            assert receipt["qualified"] is True and receipt["tracedProcesses"] >= 2
            workspace_audit = audit / str(workspace)
            (tree,) = (
                path.with_name("process-tree.jsonl")
                for path in workspace_audit.glob("*/identity.json")
                if json.loads(path.read_text())["run_id"] == str(run)
            )
            exits = [json.loads(line)["exit"] for line in tree.read_text().splitlines()]
            assert {"state": "exited", "code": 0 if clean_exit else 9} in exits
            assert not any(path.is_dir() for path in config.cgroup_root.iterdir())
            assert not list((workspace_audit / ".active").iterdir())
            with app_env.db.transaction(actor) as conn:
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
            assert service.read(actor, "agent", str(agent), "MODEL.md") == model
