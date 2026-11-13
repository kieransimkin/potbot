"""Sealed Cog, real PostgreSQL and Bubblewrap with a projected fixture image."""

import asyncio
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from potbot import __version__
from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.cogs.execution import WORK_COGS
from potbot.cogs.registry import BoundCogRegistry
from potbot.controller.execution_cogs import ExecutionCogRuntime
from potbot.controller.execution_events import ExecutionEventService
from potbot.controller.tasks import TaskService
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus
from potbot.semantics.service import SynService
from potbot.tools import python_profile_worker as worker
from potbot.tools.execution_contracts import WORK_KINDS, ExecutionPolicy
from potbot.tools.python_profile_cog import install_python_profile
from potbot.tools.repository import SnapshotProfile
from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor
from potbot.tools.shared_workspace_isolation import SharedWorkspaceRuntime
from potbot.tools.supervisor import ProcessSupervisor
from potbot.tools.syn_worker_nodes import SynWorkerNodeMesh
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger
from tests.integration.test_repository_states import setup

pytestmark = [pytest.mark.postgres, pytest.mark.sandbox]

WORKLOAD = """import socket
from pathlib import Path

assert not Path("CANARY").exists()
try:
    socket.socket()
except PermissionError:
    pass
else:
    raise AssertionError("unexpected network")

def item_cost(value):
    return (value * value) % 101

def hotspot():
    total = 0
    for value in range(200_000):
        total += item_cost(value)
    return total

print("profiling-workload-private-output")
RESULT = {"total": hotspot()}
"""


def test_sealed_python_profile_through_sandbox_worker(app_env, tmp_path: Path):
    fixture, artifacts, states, env, _, _ = setup(app_env, tmp_path)
    actor = app_env.context
    canary = tmp_path / "private-canary"
    canary.write_text("private", encoding="utf-8")
    workload = WORKLOAD.replace("CANARY", str(canary)).encode()
    source = fixture.path / "profile_target.py"
    source.write_bytes(workload)
    state = states.capture(
        actor,
        app_env.repository_id,
        fixture.path,
        env,
        profile=SnapshotProfile(untracked_paths=(source.name,)),
    )
    task_service = TaskService(app_env.db)
    task = task_service.submit(
        actor,
        app_env.repository_id,
        ContractSpec(
            objective="Measure Python profile overhead",
            baseline_ref="git:" + "a" * 40,
            deliverable_type=DeliverableType.INVESTIGATION,
            requirements=(Requirement("R1", "Find the hotspot", "Retain measured results"),),
            change_scope=(),
            capabilities=("test",),
            verification_policy="checks-v1",
            budget=(("tokens", 0), ("calls", 2), ("tool_ms", 20000), ("concurrency", 1)),
        ),
        "python-profile-task",
    )
    episode = UUID(task["task_id"])
    task_service.transition(
        actor, episode, 0, TaskStatus.INITIALIZING, "Init", idempotency_key="profile-init"
    )
    task_service.transition(
        actor, episode, 1, TaskStatus.RUNNING, "Run", idempotency_key="profile-run"
    )
    revision = UUID(task["current_contract"]["id"])
    registry = BoundCogRegistry.provenance(python_profile=True)
    sealed = SynService(app_env.db, artifacts).import_snapshot(
        actor, app_env.repository_id, "python-profile-v1", registry.snapshot
    )
    snapshot = UUID(sealed["scope"]["snapshot_id"])
    policy = ExecutionPolicy(
        "a" * 64, "b" * 64, state["content_digest"], __version__, max_wall_ms=10000
    )

    home, rootfs, private = (tmp_path / name for name in ("home", "rootfs", "private"))
    agent_home = home / "agent"
    binary = rootfs / worker.EXECUTABLE.lstrip("/")
    for path in (agent_home, binary.parent, private):
        path.mkdir(parents=True)
    (agent_home / source.name).write_bytes(workload)
    binary.write_bytes(Path(worker.__file__).read_bytes())
    binary.chmod(0o555)
    interpreter = Path(sys.executable).resolve()
    with interpreter.open("rb") as file:
        interpreter_pin = hashlib.file_digest(file, "sha256").hexdigest()
    operation = install_python_profile(
        source.name,
        hashlib.sha256(workload).hexdigest(),
        interpreter_pin,
        hashlib.sha256(binary.read_bytes()).hexdigest(),
        policy,
    )
    runtime_pin = SharedWorkspaceRuntime("b" * 64, rootfs, home, "f" * 64)
    leases = WorkspaceLeaseLedger(private / "leases.sqlite", workspace_home=home)
    leases.pin_image(
        actor.workspace_id,
        digest=runtime_pin.image_digest,
        image_ref="ghcr.io/test/system@sha256:" + runtime_pin.image_digest,
        build_commit="c" * 40,
        platform="linux/amd64",
        rootfs_path=rootfs,
        qualification_sha256=runtime_pin.qualification_sha256,
    )
    agent = uuid4()
    supervisor = ProcessSupervisor()
    launches = []

    def project_fixture(request, *, cancelled):
        # Reuse the sandbox worker and real process supervisor. Project the
        # admitted image entrypoint into CI/WSL's fixture Linux mount layout.
        actual = replace(
            request,
            argv=(str(interpreter), "-I", str(binary), *request.argv[1:]),
            guest_uid=None,
            guest_gid=None,
            cog_log_parent=None,
            read_only_roots=(binary.parent,),
        )
        result = supervisor.run(actual, cancelled=cancelled)
        assert result.dispatched and result.processes_stopped, result
        assert result.isolation_identity
        launches.append(result)
        return result

    class FixtureExecutor(SandboxedCogExecutor):
        def __init__(self):
            self.runtime, self.ledger = runtime_pin, leases
            self.homes = SimpleNamespace(
                lookup=lambda *_: SimpleNamespace(
                    workspace_id=actor.workspace_id,
                    agent_id=agent,
                    directory="agent",
                    uid=555,
                    gid=69,
                )
            )
            self.supervisor = SimpleNamespace(run=project_fixture)
            self.syn_mesh = SynWorkerNodeMesh(interval_seconds=0.05)
            self.development_syn_fixture = True
            self._context_binding = None

        def bind_context(self, database, context, operations):
            assert database is app_env.db and context == actor and operations == (operation,)

    def new_controller():
        return ExecutionCogRuntime(
            database=app_env.db,
            artifacts=artifacts,
            actor=actor,
            snapshot_id=snapshot,
            task_revision_id=revision,
            repository_state_id=state["id"],
            emitter_run_id=uuid4(),
            policy=policy,
            operations=(operation,),
            admit=lambda *_: None,
            sandbox_executor=FixtureExecutor(),
            sandbox_agent_id=agent,
        )

    controller = new_controller()
    signature = WORK_COGS[WORK_KINDS.index("script")]
    context = CogRunContext(registry.snapshot.fingerprint, revision, state["id"], uuid4())

    def call(value):
        source = TypedCogValue(
            signature.input_type_ref,
            {
                "schema_version": 1,
                "operation_ref": worker.OPERATION,
                "input": {"type_ref": worker.REQUEST, "value": value},
            },
        )
        return asyncio.run(
            registry.invoke(str(signature.cog_ref), source, context, controller=controller)
        )

    with pytest.raises(InvalidInputError):
        call({"repetitions": 2, "filename": "unadmitted.py"})
    assert not launches
    outcome = call({"repetitions": 2})
    with pytest.raises(ScopeDeniedError):
        call({"repetitions": 2})
    controller = new_controller()
    with pytest.raises(ScopeDeniedError):
        call({"repetitions": 2})
    assert len(launches) == 1
    report = outcome.value.value["result"]["value"]
    assert report["source_sha256"] == hashlib.sha256(workload).hexdigest()
    assert len(report["samples"]["profile"]) == len(report["samples"]["baseline"]) == 2
    for row in report["samples"]["profile"]:
        assert any(
            item["function"] == "item_cost" and item["calls"] == 200_000
            for item in row["functions"]
        )
        assert row["result_sha256"] == report["result_sha256"]
    assert b"profiling-workload-private-output" not in launches[0].stdout
    observed = ExecutionEventService(app_env.db).events(actor, run_id=context.invocation_id)[
        "items"
    ]
    assert [event["stage"] for event in observed].count("completed") == 1
    terminal = observed[-1]
    raw = artifacts.read(actor, UUID(terminal["output_artifact_ref"].removeprefix("artifact:")))
    assert hashlib.sha256(raw).hexdigest() == terminal["output_sha256"]
    assert json.loads(raw)["cog_receipt"] == outcome.receipt.to_dict()
