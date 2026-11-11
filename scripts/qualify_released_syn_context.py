"""Exercise native Syn admission and the legacy stdio Context RPC path."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.context.rpc_transport import RPC_TRANSPORT
from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.tools.released_syn_nodes import ReleasedSynWorkerNodeMesh
from potbot.tools.workspace_supervision import WorkspaceExecutorIdentity, WorkspaceLeaseGuard
from scripts.context_rpc_qualification_fixture import context_worker_fixture
from scripts.postgres_qualification_fixture import provisioned_app_env
from scripts.worker_audit_fixture import release_after_native_heartbeat


async def qualify_released_syn_context(
    workers: ReleasedSynWorkerNodeMesh, base: Path
) -> dict[str, Any]:
    workspace = workers.transport.workspace_id
    first, second = workers.transport.agents
    with provisioned_app_env(base / "database", workspace_id=workspace) as app_env:
        actor = app_env.context
        with context_worker_fixture(
            base / "worker",
            app_env,
            (first, second),
            workers,
            max_wall_ms=60000,
            build_commit=os.environ["R04_POTBOT_SHA"],
        ) as fixture:
            executor, operation = fixture.executor, fixture.operation
            ledger = executor.ledger
            probe = CogRunContext("a" * 64, uuid4(), uuid4(), uuid4())
            input_value = TypedCogValue(
                "syn://type/in", {"model_sha256": fixture.model(first)["sha256"]}
            )
            wrong_protocol = replace(operation, protocol="typed-json/v1")
            grant = executor.authorize_syn(wrong_protocol, input_value, probe, actor, first)
            assert grant is not None
            try:
                await executor.execute(
                    operation, input_value, probe, actor, first, syn_authorization=grant
                )
            except ScopeDeniedError:
                pass
            else:
                raise AssertionError("Typed JSON grant opened a context RPC worker")
            assert not fixture.workspace_audit.exists()
            assert not ledger.active(workspace)
            with app_env.db.transaction(actor) as conn:
                assert (
                    conn.execute(
                        "SELECT 1 FROM workspace_context_sessions WHERE run_id=%s",
                        (probe.invocation_id,),
                    ).fetchone()
                    is None
                )

            counts = []
            for agent, observer, code in ((first, second, 0), (second, first, 9)):
                context = replace(probe, invocation_id=uuid4())
                directory = fixture.directories[agent]
                model = fixture.model(agent)
                source = TypedCogValue("syn://type/in", {"model_sha256": model["sha256"]})
                args = ("hold",) if code == 0 else ("hold", "nonzero")
                selected = replace(operation, argv=(*operation.argv, *args))
                expected = {
                    "invocation_id": str(context.invocation_id),
                    "model_revision": model["revision"],
                }
                async with workers.node(workspace, observer, uuid4()) as peer:
                    authorization = executor.authorize_syn(selected, source, context, actor, agent)
                    assert authorization is not None
                    task = asyncio.create_task(
                        executor.execute(
                            selected, source, context, actor, agent, syn_authorization=authorization
                        )
                    )
                    try:
                        await release_after_native_heartbeat(
                            task,
                            peer,
                            directory,
                            fixture.workspace_audit,
                            fixture.cgroup_root,
                            agent,
                            context.invocation_id,
                        )
                        try:
                            async with asyncio.timeout(15):
                                result = await asyncio.shield(task)
                        except StateMismatchError as error:
                            if (
                                code == 0
                                or "did not complete its declared result protocol" not in str(error)
                            ):
                                raise
                        else:
                            assert code == 0 and result.type_ref == selected.binding.output_type_ref
                            assert result.value == expected
                    except Exception as error:
                        diagnostic = directory / ".qualification-fault"
                        if diagnostic.is_file():
                            error.add_note(diagnostic.read_text()[:160])
                        raise
                    finally:
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                counts.append(
                    fixture.verify_result(agent, context.invocation_id, expected, exit_code=code)
                )
                assert fixture.model(agent) == model
                if code == 0:
                    assert not ledger.active(workspace)
                else:
                    (lease,) = ledger.active(workspace)
                    assert lease["state"] == "active"
                    conflicting = WorkspaceLeaseGuard.exclusive_home(
                        ledger,
                        WorkspaceExecutorIdentity(workspace, first, executor.runtime.image_digest),
                        operation="cog.sandbox.execute",
                        idempotency_key="cog-" + uuid4().hex,
                    )
                    try:
                        ledger.claim(conflicting.request)
                    except StateMismatchError as error:
                        assert "conflicting or indeterminate" in str(error)
                    else:
                        raise AssertionError(
                            "Nonzero context worker exit released its workspace fence"
                        )

    return {
        "route": "context-rpc/v1",
        "rpc_transport": RPC_TRANSPORT,
        "syn_rpc_transport_verified": False,
        "audited_exit_codes": [0, 9],
        "traced_process_counts": counts,
        "native_peer_heartbeat_while_worker_live": True,
        "typed_json_grant_refused_before_dispatch": True,
        "postgresql_results_persisted_and_sessions_revoked": True,
        "clean_exit_released_lease": True,
        "failed_exit_kept_workspace_fenced": True,
    }
