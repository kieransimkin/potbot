"""Positive and negative Cog dispatch against a live, official Syn cohort.

Called only by the pinned upstream disposable qualification fixture. The
fixture owns both native nodes and their relay until this probe returns.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.context.service import WorkspaceContextService
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.persistence.database import Database, migrate, provision_principal, provision_workspace
from potbot.persistence.harness import TestDatabaseConfig
from potbot.tools.agent_homes import AgentHomeRegistry
from potbot.tools.ai_config_history import AiConfigHistoryMonitor
from potbot.tools.execution_contracts import ExecutionPolicy, WorkBinding
from potbot.tools.gui_audit_verification import verify_gui_run
from potbot.tools.released_syn_nodes import ReleasedSynWorkerNodeMesh, SynCogGrant
from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor, SandboxedCogOperation
from potbot.tools.secrets import CaptureRedactor
from potbot.tools.shared_workspace_isolation import (
    SharedWorkspaceBubblewrapBackend,
    SharedWorkspaceRuntime,
)
from potbot.tools.supervisor import ProcessRequest, ProcessResult, ProcessSupervisor
from potbot.tools.syn_worker_nodes import SynWorkerNode
from potbot.tools.worker_telemetry import _cgroup_populated
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger
from potbot.tools.workspace_supervision import WorkspaceExecutorIdentity, WorkspaceLeaseGuard
from scripts.worker_audit_fixture import monitored_cog_cgroup, prepare_busybox_rootfs

_PROBE = """#!/bin/sh
set -eu
IFS= read -r request || test -n "$request"
: > .qualification-ready
until [ -f .qualification-release ]; do /bin/busybox sleep 0.25; done
printf '%s\\n' '{"schema_version":1,"type_ref":"syn://type/fixture-output","value":{"done":true}}'
"""
_CONTEXT_ENTRYPOINT = "/usr/local/tools/qualification/static/context-worker"


async def _qualify_context_rpc(
    executor: SandboxedCogExecutor,
    audit: Path,
    cgroup: Path,
    binary: Path,
    workspace: UUID,
    agents: tuple[UUID, UUID],
) -> dict[str, Any]:
    import psycopg
    from psycopg import sql

    config = TestDatabaseConfig.from_env()
    parsed = urlsplit(config.dsn)
    database = "potbot_test_" + uuid4().hex
    role = "potbot_app_" + uuid4().hex
    password = secrets.token_hex(24)
    admin = urlunsplit(parsed._replace(path=f"/{database}"))
    dsn = urlunsplit(
        parsed._replace(netloc=f"{role}:{password}@127.0.0.1:{config.port}", path=f"/{database}")
    )
    created_database = created_role = False
    try:
        with psycopg.connect(config.dsn, autocommit=True, connect_timeout=5) as conn:
            conn.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database))
            )
            created_database = True
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS NOINHERIT"
                ).format(sql.Identifier(role), sql.Literal(password))
            )
            created_role = True
        migrate(admin)
        provision_workspace(
            admin,
            "potbot",
            "official-context-audit",
            workspace,
            sandbox_root=binary.parents[3] / "context-db",
        )
        actor = provision_principal(admin, "potbot", role, workspace)
        db = Database(dsn)
        service = WorkspaceContextService(db)
        worker_sha = hashlib.sha256(binary.read_bytes()).hexdigest()
        operation = SandboxedCogOperation(
            WorkBinding(
                "tool",
                "syn://thing/context-audit-fixture",
                "syn://type/in",
                "syn://type/out",
                worker_sha,
                "read_only",
                ExecutionPolicy("d" * 64, "e" * 64, "f" * 64, "test", max_wall_ms=30000),
            ),
            (_CONTEXT_ENTRYPOINT,),
            worker_sha,
            protocol="context-rpc/v1",
        )
        executor.bind_context(db, actor, (operation,))
        for agent in agents:
            service.provision_private(actor, "agent", str(agent), idempotency_key=uuid4().hex)
        results = []
        for agent, clean in ((agents[0], True), (agents[1], True), (agents[0], False)):
            model = service.read(actor, "agent", str(agent), "MODEL.md")
            value = TypedCogValue(
                "syn://type/in", {"model_sha256": model["sha256"], "nonzero": not clean}
            )
            run = uuid4()
            context = CogRunContext("a" * 64, uuid4(), uuid4(), run)
            expected = {"invocation_id": str(run), "model_revision": model["revision"]}
            grant = executor.authorize_syn(operation, value, context, actor, agent)
            assert grant is not None
            try:
                output = await executor.execute(
                    operation, value, context, actor, agent, syn_authorization=grant
                )
            except StateMismatchError as error:
                registered = executor.homes.lookup(workspace, agent)
                assert registered is not None
                diagnostic = executor.runtime.home / registered.directory / ".qualification-fault"
                if clean or "did not complete its declared result" not in str(error):
                    if diagnostic.is_file():
                        error.add_note(diagnostic.read_text()[:160])
                    raise
                (retained,) = executor.ledger.active(workspace)
                assert retained["state"] == "active"
                guard = WorkspaceLeaseGuard.exclusive_home(
                    executor.ledger,
                    WorkspaceExecutorIdentity(workspace, agent, executor.runtime.image_digest),
                    operation="cog.sandbox.execute",
                    idempotency_key="cog-" + uuid4().hex,
                )
                try:
                    executor.ledger.claim(guard.request)
                except StateMismatchError as refusal:
                    assert "conflicting or indeterminate" in str(refusal)
                else:
                    raise AssertionError("A nonzero context worker failed to fence the workspace")
            else:
                assert clean and output.type_ref == "syn://type/out" and output.value == expected
                assert not executor.ledger.active(workspace)
            receipt = verify_gui_run(audit, workspace, run, identity_kind="agent", subject_id=agent)
            assert receipt["qualified"] is True and receipt["tracedProcesses"] >= 2
            workspace_audit = audit / str(workspace)
            (tree,) = (
                path.with_name("process-tree.jsonl")
                for path in workspace_audit.glob("*/identity.json")
                if json.loads(path.read_text())["run_id"] == str(run)
            )
            assert {"state": "exited", "code": 0 if clean else 9} in [
                json.loads(line)["exit"] for line in tree.read_text().splitlines()
            ]
            assert not any(path.is_dir() for path in cgroup.iterdir())
            assert not list((workspace_audit / ".active").iterdir())
            with db.transaction(actor) as conn:
                record = conn.execute(
                    "SELECT s.agent_id,s.state,w.result_json::jsonb AS result "
                    "FROM workspace_context_sessions s JOIN workspace_context_work_results w "
                    "ON s.workspace_id=w.workspace_id AND s.id=w.session_id "
                    "AND s.result_request_id=w.request_id WHERE s.run_id=%s",
                    (run,),
                ).fetchone()
            assert record and record["agent_id"] == agent and record["state"] == "revoked"
            assert record["result"] == {
                "schema_version": 1,
                "type_ref": "syn://type/out",
                "value": expected,
            }
            assert service.read(actor, "agent", str(agent), "MODEL.md") == model
            results.append(
                {"exit_code": 0 if clean else 9, "audited_processes": receipt["tracedProcesses"]}
            )
        return {"protocol": "context-rpc/v1", "result": "passed", "postgresql_cases": results}
    finally:
        with psycopg.connect(config.dsn, autocommit=True, connect_timeout=5) as conn:
            if created_database:
                conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))
            if created_role:
                conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def _signed_image_receipt() -> tuple[Path, dict[str, str]] | None:
    rootfs = os.environ.get("POTBOT_QUALIFICATION_SIGNED_ROOTFS")
    receipt = os.environ.get("POTBOT_QUALIFICATION_SIGNED_RECEIPT")
    if rootfs is None and receipt is None:
        return None
    if not rootfs or not receipt:
        raise ValueError("The verified rootfs and its live smoke receipt are both required")
    path = Path(rootfs)
    if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_dir():
        raise ValueError("The verified rootfs must be a real absolute directory")
    report = json.loads(Path(receipt).read_text(encoding="utf-8"))
    if (
        report["schema"] != "potbot.published-image-hosted-smoke/1"
        or report["qualified_smoke"] is not True
        or report["operator_gate"]["qualified"] is not True
        or report["consumer_commit"] != os.environ["R04_POTBOT_SHA"]
        or report["kernel_release"] != os.uname().release
    ):
        raise ValueError("Signed image smoke did not qualify this consumer and kernel")
    workspace = dict(report["workspace_smoke"])
    qualification = workspace.pop("receipt_sha256")
    calculated = hashlib.sha256(
        json.dumps(workspace, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if (
        workspace["qualified_smoke"] is not True
        or workspace["image_digest"] != report["image_digest"]
        or calculated != qualification
    ):
        raise ValueError("Signed image workspace receipt does not match")
    for relative in ("opt/potbot/context_client.py", "usr/local/libexec/potbot-worker-probe"):
        installed = path / relative
        if (
            installed.resolve(strict=True) != installed
            or hashlib.sha256(installed.read_bytes()).hexdigest()
            != report["shipped_sha256"][relative]
        ):
            raise ValueError("Rootfs does not match the published image smoke receipt")
    return path, {
        "digest": report["image_digest"],
        "source_commit": report["image_source_commit"],
        "qualification_sha256": qualification,
    }


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
    signed = _signed_image_receipt()
    if signed is None:
        prepare_busybox_rootfs(image)
    for path in (home / ".tmp", staging, models, tools, private, audit):
        path.mkdir(mode=0o700 if path in (private, audit) else 0o755, parents=True)
    config_ai = history_ai = None
    watcher_context: AbstractContextManager[AiConfigHistoryMonitor | None] = nullcontext()
    image_metadata = None
    if signed is None:
        entrypoint = "/bin/qualification-worker"
        binary = image / entrypoint.lstrip("/")
        script = _PROBE
    else:
        image, image_metadata = signed
        entrypoint = "/usr/local/tools/qualification/static/worker"
        binary = tools / entrypoint.removeprefix("/usr/local/tools/")
        binary.parent.mkdir(parents=True)
        script = _PROBE.replace("/bin/busybox sleep", "/usr/bin/sleep")
        config_ai, history_ai = fixture / "ai", fixture / "history"
        config_ai.mkdir()
        config_ai.chmod(0o775)
        history_ai.mkdir()
        watcher_context = AiConfigHistoryMonitor(config_dir=config_ai, past_dir=history_ai)
    binary.write_text(script, encoding="utf-8")
    binary.chmod(0o555)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    entrypoints = [] if image_metadata is None else [(entrypoint, digest)]
    context_binary = None
    context_result = None
    if os.environ.get("POTBOT_QUALIFICATION_CONTEXT_RPC") == "1":
        if image_metadata is None:
            raise ValueError("Official context RPC qualification requires the signed worker image")
        # The upstream driver imports this probe from the official Syn venv.
        # Append the consumer's locked test dependencies without changing its Syn imports.
        python_dir = f"python{sys.version_info.major}.{sys.version_info.minor}"
        dependencies = (
            Path(__file__).resolve().parents[1] / ".venv/lib" / python_dir / "site-packages"
        )
        if not dependencies.is_dir():
            raise ValueError("The Potbot qualification environment is missing")
        sys.path.append(str(dependencies))
        from tests.integration.test_monitored_context_rpc import _PROBE as context_probe

        context_binary = tools / _CONTEXT_ENTRYPOINT.removeprefix("/usr/local/tools/")
        assert 'sys.argv[1:] == ["nonzero"]' in context_probe
        context_probe = context_probe.replace(
            'sys.argv[1:] == ["nonzero"]', 'work["input"]["value"]["nonzero"] is True'
        )
        context_binary.write_text("#!/usr/bin/python3 -I\n" + context_probe, encoding="utf-8")
        context_binary.chmod(0o555)
        entrypoints.append(
            (_CONTEXT_ENTRYPOINT, hashlib.sha256(context_binary.read_bytes()).hexdigest())
        )
    runtime = SharedWorkspaceRuntime(
        digest if image_metadata is None else image_metadata["digest"],
        image,
        home,
        "b" * 64 if image_metadata is None else image_metadata["qualification_sha256"],
        model_staging=staging,
        shared_models=models,
        shared_tools=tools,
        tool_entrypoints=tuple(entrypoints),
        ai_config_dir=config_ai,
        ai_history_dir=history_ai,
    )
    ledger = WorkspaceLeaseLedger(private / "leases.sqlite", workspace_home=home)
    ledger.pin_image(
        workspace,
        digest=runtime.image_digest,
        image_ref=(
            f"ghcr.io/test/image@sha256:{runtime.image_digest}"
            if image_metadata is None
            else f"ghcr.io/kieransimkin/potbot-sandbox@sha256:{runtime.image_digest}"
        ),
        build_commit=(
            os.environ["R04_POTBOT_SHA"]
            if image_metadata is None
            else image_metadata["source_commit"]
        ),
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
            ExecutionPolicy("d" * 64, "e" * 64, "f" * 64, "test", max_wall_ms=60000),
        ),
        (entrypoint,),
        digest,
    )
    value = TypedCogValue("syn://type/fixture-input", {"fixture": True})
    workspace_audit = audit / str(workspace)

    class ObservedSupervisor(ProcessSupervisor):
        def run(
            self,
            request: ProcessRequest,
            *,
            cancelled: Callable[[], bool] | None = None,
            redactor: CaptureRedactor | None = None,
        ) -> ProcessResult:
            result = super().run(request, cancelled=cancelled, redactor=redactor)
            if result.outcome != "completed":
                print(
                    "Official worker stopped:",
                    result.outcome,
                    result.error_code,
                    f"elapsed_ms={result.elapsed_ms}",
                    flush=True,
                )
            return result

    with monitored_cog_cgroup(private, audit) as config, watcher_context as watcher:
        executor = SandboxedCogExecutor(
            runtime,
            ledger,
            homes,
            ObservedSupervisor(
                SharedWorkspaceBubblewrapBackend(runtime, ai_monitor=watcher),
                monitor_config=config,
            ),
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
                    try:
                        async with asyncio.timeout(45):
                            while True:
                                await require_running(task)
                                entries = list((workspace_audit / ".active").glob("*.json"))
                                if entries and (directory / ".qualification-ready").is_file():
                                    break
                                await asyncio.sleep(0.05)
                    except TimeoutError as exc:
                        raise AssertionError(
                            "Authorized Cog did not reach its live cgroup"
                        ) from exc

                    (active,) = entries
                    leaf = config.cgroup_root / UUID(active.stem).hex
                    assert _cgroup_populated(leaf)
                    baseline = observed_sequence(peer, agent, context.invocation_id)
                    try:
                        async with asyncio.timeout(24):
                            while observed_sequence(peer, agent, context.invocation_id) <= max(
                                baseline, 1
                            ):
                                await require_running(task)
                                assert peer.healthy, (
                                    "Native peer stopped during live Cog observation"
                                )
                                await asyncio.sleep(0.05)
                    except TimeoutError as exc:
                        raise AssertionError(
                            "Native peer missed a new heartbeat from the live Cog"
                        ) from exc
                    assert _cgroup_populated(leaf)
                    (directory / ".qualification-release").touch()
                    async with asyncio.timeout(15):
                        result = await asyncio.shield(task)
                    assert result.type_ref == operation.binding.output_type_ref
                    assert result.value == {"done": True}
                except Exception:
                    print(
                        "Official worker observation:",
                        f"transport_healthy={transport.healthy}",
                        f"peer_healthy={peer.healthy}",
                        f"sequence={observed_sequence(peer, agent, context.invocation_id)}",
                        flush=True,
                    )
                    raise
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

        if context_binary is not None:
            context_result = await _qualify_context_rpc(
                executor, audit, config.cgroup_root, context_binary, workspace, (first, second)
            )

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
        **({"worker_image": image_metadata} if image_metadata is not None else {}),
        **({"context_rpc": context_result} if context_result is not None else {}),
    }
