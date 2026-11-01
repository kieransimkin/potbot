"""Real PostgreSQL/controller journal with a scripted browser transport.

This exercises sealed browser Cogs, admission, private ArtifactStore bytes and
terminal event persistence. It does NOT launch Chromium, Xpra or a sandbox and
must not be reported as live-browser/host qualification.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from potbot import __version__
from potbot.cogs.browser import ACT, BROWSER_COGS, CAPTURE, OBSERVE, PICK
from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.cogs.registry import BoundCogRegistry
from potbot.controller.browser_cogs import BrowserControlService
from potbot.controller.execution_cogs import ExecutionCogRuntime
from potbot.controller.execution_events import ExecutionEventService
from potbot.controller.execution_journal import PinnedExecutionJournal
from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.semantics.service import SynService
from potbot.tools.execution_contracts import ExecutionPolicy
from tests.integration.test_repository_states import setup
from tests.unit.test_browser_cogs_s37 import JPEG, SESSION, Apps, request

pytestmark = pytest.mark.postgres


def configured(app_env, tmp_path, *, deny_at=None):
    _, artifacts, _, _, state, _ = setup(app_env, tmp_path)
    registry = BoundCogRegistry.browser()
    sealed = SynService(app_env.db, artifacts).import_snapshot(
        app_env.context,
        app_env.repository_id,
        "browser-controller-s48",
        registry.snapshot,
    )
    selected_policy = ExecutionPolicy("a" * 64, "b" * 64, state["content_digest"], __version__)
    apps = Apps(app_env.context)  # Only the Xpra RPC/stop boundary is scripted.
    service = BrowserControlService(
        actor=app_env.context,
        apps=apps,
        artifacts=artifacts,
        policy=selected_policy,
        approved_sessions=(SESSION,),
    )
    task, admissions = uuid4(), []

    def admit(*_args):
        admissions.append(len(admissions) + 1)
        if len(admissions) == deny_at:
            raise ScopeDeniedError("fixture policy revoked")

    def runtime():
        return ExecutionCogRuntime(
            database=app_env.db,
            artifacts=artifacts,
            actor=app_env.context,
            snapshot_id=UUID(sealed["scope"]["snapshot_id"]),
            task_revision_id=task,
            repository_state_id=state["id"],
            emitter_run_id=uuid4(),
            policy=selected_policy,
            operations=(),
            admit=admit,
            browser_service=service,
        )

    return SimpleNamespace(
        app=app_env,
        artifacts=artifacts,
        state=state,
        registry=registry,
        apps=apps,
        service=service,
        task=task,
        host=runtime(),
        runtime=runtime,
        admissions=admissions,
    )


def context(fixture):
    return CogRunContext(
        fixture.registry.snapshot.fingerprint,
        fixture.task,
        fixture.state["id"],
        uuid4(),
    )


def invoke(fixture, signature, selected, *, run=None):
    run = run or context(fixture)
    result = asyncio.run(
        fixture.registry.invoke(
            str(signature.cog_ref),
            TypedCogValue(signature.input_type_ref, request(selected)),
            run,
            controller=fixture.host,
        )
    )
    return run, result


def events(fixture, run):
    page = ExecutionEventService(fixture.app.db).events(
        fixture.app.context,
        run_id=run.invocation_id,
        limit=200,
    )
    assert page["has_more"] is False
    return page["items"]


def test_all_four_registered_browser_cogs_persist_causal_events_and_private_evidence(
    app_env,
    tmp_path,
):
    fixture = configured(app_env, tmp_path)
    commands = (
        {"op": "accessibility"},
        {"op": "role.click", "role": "button", "name": "Submit"},
        {"op": "inspect.start"},
        {"op": "sample", "image": True},
    )
    assert fixture.host.operations == {}  # No arbitrary HostOperation worker fixture.
    for signature, selected in zip(BROWSER_COGS, commands, strict=True):
        if signature == CAPTURE:
            fixture.apps.result = {
                "frame": {
                    "mime": "image/jpeg",
                    "sha256": hashlib.sha256(JPEG).hexdigest(),
                    "bytes": len(JPEG),
                    "base64": base64.b64encode(JPEG).decode(),
                }
            }
        run, outcome = invoke(fixture, signature, selected)
        receipt = outcome.value.value
        history = events(fixture, run)
        assert history[0]["stage"] == "started" and history[-1]["stage"] == "completed"
        assert sum(row["stage"] == "completed" for row in history) == 1
        for previous, current in zip(history[:-1], history[1:], strict=True):
            assert current["parent_event_id"] == previous["id"]
            assert current["parent_run_id"] == str(run.invocation_id)
        for row in history:
            assert (
                row["telemetry"]["semantic_snapshot_hash"] == fixture.registry.snapshot.fingerprint
            )
            assert row["telemetry"]["execution_scope_hash"] == outcome.receipt.scope_hash
            assert row["telemetry"]["work_kind"] == "browser"
        for prefix in ("observation", "manifest"):
            raw = fixture.artifacts.read(app_env.context, UUID(receipt[prefix + "_artifact_id"]))
            assert hashlib.sha256(raw).hexdigest() == receipt[prefix + "_sha256"]
        manifest = json.loads(
            fixture.artifacts.read(
                app_env.context,
                UUID(receipt["manifest_artifact_id"]),
            )
        )
        assert manifest["invocation_id"] == str(run.invocation_id)
        assert manifest["workspace_id"] == str(app_env.context.workspace_id)
        assert manifest["principal_id"] == str(app_env.context.principal_id)
        assert receipt["preview"] is None
        assert "base64" not in json.dumps(receipt)
        terminal = history[-1]
        output = fixture.artifacts.read(
            app_env.context,
            UUID(terminal["output_artifact_ref"].removeprefix("artifact:")),
        )
        assert hashlib.sha256(output).hexdigest() == terminal["output_sha256"]
        assert json.loads(output)["cog_receipt"] == outcome.receipt.to_dict()
        assert (
            ExecutionEventService(app_env.other_db).events(
                app_env.other_context,
                run_id=run.invocation_id,
            )["items"]
            == []
        )
        if signature == CAPTURE:
            assert (
                fixture.artifacts.read(
                    app_env.context,
                    UUID(receipt["image_artifact_id"]),
                )
                == JPEG
            )
        else:
            assert receipt["image_artifact_id"] is None
        before = len(fixture.apps.commands)
        with pytest.raises(ScopeDeniedError):
            invoke(fixture, signature, selected, run=run)
        assert len(fixture.apps.commands) == before
    assert len(fixture.apps.commands) == 4 and fixture.apps.fenced == []


def test_completed_action_with_failed_journal_is_indeterminate_and_fenced(
    app_env,
    tmp_path,
    monkeypatch,
):
    fixture = configured(app_env, tmp_path)
    run = context(fixture)
    original = PinnedExecutionJournal.append

    def reject_completion(self, actor, event):
        if event.run_id == run.invocation_id and event.stage == "completed":
            raise OSError("fixture completion persistence unavailable")
        return original(self, actor, event)

    monkeypatch.setattr(PinnedExecutionJournal, "append", reject_completion)
    with pytest.raises(OSError, match="completion persistence"):
        invoke(fixture, ACT, {"op": "navigate", "url": "about:blank"}, run=run)
    history = events(fixture, run)
    assert history[-1]["stage"] == "indeterminate"
    assert all(row["stage"] != "completed" for row in history)
    assert SESSION in fixture.apps.fenced and len(fixture.apps.commands) == 1
    fixture.host = fixture.runtime()
    with pytest.raises(StateMismatchError, match="session is fenced"):
        invoke(fixture, OBSERVE, {"op": "state"})
    assert len(fixture.apps.commands) == 1


def test_policy_revoked_after_picker_effect_fences_without_replaying(app_env, tmp_path):
    fixture = configured(app_env, tmp_path, deny_at=4)
    run = context(fixture)
    with pytest.raises(ScopeDeniedError, match="policy revoked"):
        invoke(fixture, PICK, {"op": "inspect.start"}, run=run)
    assert events(fixture, run)[-1]["stage"] == "indeterminate"
    assert SESSION in fixture.apps.fenced and len(fixture.apps.commands) == 1


def test_readonly_observation_journal_failure_is_failed_not_an_action_replay(
    app_env,
    tmp_path,
    monkeypatch,
):
    fixture = configured(app_env, tmp_path)
    run = context(fixture)
    original = PinnedExecutionJournal.append

    def reject_completion(self, actor, event):
        if event.run_id == run.invocation_id and event.stage == "completed":
            raise OSError("fixture read-only completion failure")
        return original(self, actor, event)

    monkeypatch.setattr(PinnedExecutionJournal, "append", reject_completion)
    with pytest.raises(OSError):
        invoke(fixture, OBSERVE, {"op": "state"}, run=run)
    assert events(fixture, run)[-1]["stage"] == "failed"
    assert fixture.apps.fenced == [] and len(fixture.apps.commands) == 1


def test_initial_admission_denial_records_failure_without_browser_dispatch(app_env, tmp_path):
    fixture = configured(app_env, tmp_path, deny_at=1)
    run = context(fixture)
    with pytest.raises(ScopeDeniedError):
        invoke(fixture, ACT, {"op": "navigate", "url": "about:blank"}, run=run)
    history = events(fixture, run)
    assert len(history) == 1 and history[0]["stage"] == "failed"
    assert history[0]["telemetry"]["phase"] == "admission_denied"
    assert fixture.apps.commands == [] and fixture.apps.fenced == []


def test_wrong_catalogue_or_repository_scope_is_rejected_before_dispatch(app_env, tmp_path):
    fixture = configured(app_env, tmp_path)
    original = context(fixture)
    altered = (
        replace(original, cog_snapshot_hash=BoundCogRegistry.provenance().snapshot.fingerprint),
        replace(original, repository_state_id=uuid4()),
        replace(original, task_revision_id=uuid4()),
    )
    for run in altered:
        with pytest.raises((ScopeDeniedError, StateMismatchError)):
            invoke(fixture, OBSERVE, {"op": "state"}, run=run)
    assert fixture.apps.commands == [] and fixture.apps.fenced == []
