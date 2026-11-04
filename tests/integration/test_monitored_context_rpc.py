"""Qualify the installed context RPC profile with live Linux audit and PostgreSQL."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.domain.errors import StateMismatchError
from potbot.tools import workspace_concurrency
from potbot.tools.syn_worker_nodes import SynWorkerNodeMesh
from potbot.tools.workspace_supervision import WorkspaceExecutorIdentity, WorkspaceLeaseGuard
from scripts.context_rpc_qualification_fixture import context_worker_fixture
from tests.integration.conftest import AppEnvironment
from tests.integration.test_worker_audit_linux import monitored_cog_cgroup


@pytest.mark.postgres
@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("POTBOT_TEST_WORKER_CGROUP") != "1",
    reason="requires disposable Linux cgroup, TASKSTATS and PostgreSQL",
)
def test_registered_context_rpc_result_requires_audited_worker_exit(
    tmp_path: Path, app_env: AppEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = app_env.context
    workspace = actor.workspace_id
    agent = uuid4()
    with context_worker_fixture(
        tmp_path,
        app_env,
        (agent,),
        SynWorkerNodeMesh(),
        development_syn_fixture=True,
        monitor=monitored_cog_cgroup,
    ) as fixture:
        executor, op = fixture.executor, fixture.operation
        ledger = executor.ledger
        diagnostic = fixture.directories[agent] / ".qualification-fault"
        model = fixture.model(agent)
        value = TypedCogValue("syn://type/in", {"model_sha256": model["sha256"]})
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
                assert retained["state"] == "active"
                next_work = WorkspaceLeaseGuard.exclusive_home(
                    ledger,
                    WorkspaceExecutorIdentity(workspace, agent, executor.runtime.image_digest),
                    operation="cog.sandbox.execute",
                    idempotency_key="cog-" + uuid4().hex,
                )
                with pytest.raises(StateMismatchError, match="conflicting or indeterminate"):
                    ledger.claim(next_work.request)
                expires = retained["expires_ns"]
                assert type(expires) is int
                # Advance only the ledger clock after the real worker has stopped.
                with monkeypatch.context() as clock:
                    clock.setattr(
                        workspace_concurrency,
                        "time",
                        SimpleNamespace(time_ns=lambda now=expires + 1: now),
                    )
                    (expired,) = ledger.active(workspace)
                    assert expired["state"] == "indeterminate"
                    with pytest.raises(StateMismatchError, match="conflicting or indeterminate"):
                        ledger.claim(next_work.request)

            fixture.verify_result(agent, run, expected, exit_code=0 if clean_exit else 9)
            assert fixture.model(agent) == model
