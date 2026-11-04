"""Positive and negative Cog dispatch against a live, official Syn cohort.

Called only by the pinned upstream disposable qualification fixture. The
fixture owns both native nodes and their relay until this probe returns.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError
from potbot.tools.agent_homes import AgentHomeRegistry
from potbot.tools.execution_contracts import ExecutionPolicy, WorkBinding
from potbot.tools.gui_audit_verification import verify_gui_run
from potbot.tools.released_syn_nodes import ReleasedSynWorkerNodeMesh, SynCogGrant
from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor, SandboxedCogOperation
from potbot.tools.shared_workspace_isolation import (
    SharedWorkspaceBubblewrapBackend,
    SharedWorkspaceRuntime,
)
from potbot.tools.supervisor import ProcessSupervisor
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger
from scripts.worker_audit_fixture import monitored_cog_cgroup, prepare_busybox_rootfs

_PROBE = """#!/bin/sh
set -eu
IFS= read -r request || test -n "$request"
: > .qualification-ready
until [ -f .qualification-release ]; do /bin/busybox sleep 0.25; done
printf '%s\\n' '{"schema_version":1,"type_ref":"syn://type/fixture-output","value":{"done":true}}'
"""


async def qualify_monitored_workers(
    workers: ReleasedSynWorkerNodeMesh, base: Path
) -> dict[str, Any]:
    """Qualify the real typed JSON route for two Agent identities in one cohort."""
    transport = workers.transport
    workspace = transport.workspace_id
    actor = WorkspaceContext(workspace, uuid4())
    first, second = transport.agents
    expected = {
        "version": os.environ["R04_VERSION"],
        "source_commit": os.environ["R04_RELEASE_SOURCE"],
        "manifest_sha256": os.environ["R04_MANIFEST"],
    }
    authority = os.environ["R04_RELEASE_AUTHORITY"]
    for agent in (first, second):
        assert all(transport.provenance(agent)[key] == value for key, value in expected.items())
        assert (
            transport.participant(agent).deployment.policy.value["release_key_sha256"] == authority
        )

    fixture = base / "monitored-workers"
    image, home, staging, models, tools, private, audit = (
        fixture / name
        for name in ("image", "home", "staging", "models", "tools", "private", "audit")
    )
    prepare_busybox_rootfs(image)
    for path in (home / ".tmp", staging, models, tools, private, audit):
        path.mkdir(mode=0o700 if path in (private, audit) else 0o755, parents=True)
    binary = image / "bin/qualification-worker"
    binary.write_text(_PROBE, encoding="utf-8")
    binary.chmod(0o555)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    runtime = SharedWorkspaceRuntime(
        digest,
        image,
        home,
        "b" * 64,
        model_staging=staging,
        shared_models=models,
        shared_tools=tools,
    )
    ledger = WorkspaceLeaseLedger(private / "leases.sqlite", workspace_home=home)
    ledger.pin_image(
        workspace,
        digest=runtime.image_digest,
        image_ref=f"ghcr.io/test/image@sha256:{runtime.image_digest}",
        build_commit=os.environ["R04_POTBOT_SHA"],
        platform="linux/amd64",
        rootfs_path=image,
        qualification_sha256=runtime.qualification_sha256,
    )
    homes = AgentHomeRegistry(ledger, workspace_name="official-syn-audit")
    agent_homes = {agent: homes.provision(workspace, agent) for agent in transport.agents}
    operation = SandboxedCogOperation(
        WorkBinding(
            "tool",
            "syn://thing/official-monitored-fixture",
            "syn://type/fixture-input",
            "syn://type/fixture-output",
            digest,
            "read_only",
            ExecutionPolicy("d" * 64, "e" * 64, "f" * 64, "test", max_wall_ms=25000),
        ),
        ("/bin/qualification-worker",),
        digest,
    )
    value = TypedCogValue("syn://type/fixture-input", {"fixture": True})
    workspace_audit = audit / str(workspace)

    with monitored_cog_cgroup(private, audit) as config:
        executor = SandboxedCogExecutor(
            runtime,
            ledger,
            homes,
            ProcessSupervisor(SharedWorkspaceBubblewrapBackend(runtime), monitor_config=config),
            workers,
        )

        async def reject(context: CogRunContext, agent: UUID, grant: SynCogGrant | None) -> None:
            before = set(workspace_audit.glob("*/identity.json"))
            try:
                await executor.execute(
                    operation, value, context, actor, agent, syn_authorization=grant
                )
            except ScopeDeniedError:
                pass
            else:
                raise AssertionError("Invalid Syn grant reached the worker")
            assert set(workspace_audit.glob("*/identity.json")) == before
            assert not ledger.active(workspace)
            assert not any(path.is_dir() for path in config.cgroup_root.iterdir())

        probe = CogRunContext("a" * 64, uuid4(), uuid4(), uuid4())
        await reject(probe, first, None)
        wrong_task = executor.authorize_syn(operation, value, probe, actor, first)
        assert wrong_task is not None
        await reject(replace(probe, task_revision_id=uuid4()), first, wrong_task)
        wrong_agent = executor.authorize_syn(operation, value, probe, actor, first)
        assert wrong_agent is not None
        await reject(probe, second, wrong_agent)

        verified = []
        for agent, observer in ((first, second), (second, first)):
            context = replace(probe, invocation_id=uuid4())
            directory = home / agent_homes[agent].directory
            async with workers.node(workspace, observer, uuid4()) as peer:
                grant = executor.authorize_syn(operation, value, context, actor, agent)
                assert grant is not None
                task = asyncio.create_task(
                    executor.execute(
                        operation, value, context, actor, agent, syn_authorization=grant
                    )
                )
                try:
                    async with asyncio.timeout(16):
                        while True:
                            if task.done():
                                await task
                                raise AssertionError("Worker exited before native observation")
                            peers = peer.snapshot()["peers"]
                            received = isinstance(peers, list) and any(
                                beat["agent_id"] == str(agent)
                                and beat["worker_run_id"] == str(context.invocation_id)
                                and beat["last_sequence"] >= 2
                                and beat["state"] == "live"
                                for beat in peers
                            )
                            if (directory / ".qualification-ready").is_file() and received:
                                break
                            await asyncio.sleep(0.05)
                    (active,) = (workspace_audit / ".active").glob("*.json")
                    assert (config.cgroup_root / UUID(active.stem).hex).is_dir()
                    (directory / ".qualification-release").touch()
                    async with asyncio.timeout(8):
                        result = await asyncio.shield(task)
                    assert result.type_ref == operation.binding.output_type_ref
                    assert result.value == {"done": True}
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            receipt = verify_gui_run(
                audit, workspace, context.invocation_id, identity_kind="agent", subject_id=agent
            )
            assert receipt["qualified"] is True and receipt["tracedProcesses"] >= 3
            assert not list((workspace_audit / ".active").iterdir())
            assert not ledger.active(workspace)
            verified.append(receipt["tracedProcesses"])
            await reject(context, agent, grant)  # The successful grant was consumed.

    return {
        "profile": "potbot.official-syn-monitored-workers/1",
        "result": "passed",
        "release": {**expected, "release_key_sha256": authority},
        "consumer_sha": os.environ["R04_POTBOT_SHA"],
        "route": "typed-json/v1",
        "audited_agents": len(verified),
        "traced_process_counts": verified,
        "native_peer_heartbeat_while_worker_live": True,
        "missing_changed_task_cross_agent_and_replayed_grants_refused": True,
        "workspace_leases_and_worker_cgroups_released": True,
    }
