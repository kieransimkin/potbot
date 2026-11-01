"""Browser catalogue admission and post-dispatch failure contracts.

The runtime tests double the journal *transport*, not the production invocation
method, browser handler, service or evidence recorder. They do not qualify Xpra.
The separate PostgreSQL integration tests exercise the real durable journal.
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from potbot.cogs.artifact_ranges import RANGE_COGS, artifact_range_snapshot
from potbot.cogs.artifact_uploads import UPLOAD_COGS, artifact_upload_snapshot
from potbot.cogs.browser import (
    ACT,
    BROWSER_COGS,
    CAPTURE,
    OBSERVE,
    PICK,
    browser_cog_snapshot,
    installed_operations,
)
from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.cogs.execution import EXECUTION_COGS, execution_cog_snapshot
from potbot.controller import execution_cogs
from potbot.controller.execution_cogs import ExecutionCogRuntime
from potbot.controller.execution_journal import PinnedExecutionJournal
from potbot.domain.errors import (
    IndeterminateExecutionError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.tools.browser_cog_contracts import EVIDENCE_RESULT, schemas
from potbot.tools.execution_contracts import WorkBinding
from tests.unit.test_browser_cogs_s37 import SESSION, policy, request, setup

PROFILES = (
    (execution_cog_snapshot, EXECUTION_COGS),
    (artifact_range_snapshot, (*EXECUTION_COGS, *RANGE_COGS)),
    (artifact_upload_snapshot, (*EXECUTION_COGS, *RANGE_COGS, *UPLOAD_COGS)),
    (browser_cog_snapshot, (*EXECUTION_COGS, *BROWSER_COGS)),
)


def journal(signature, snapshot, *, source=None):
    selected_policy = policy()
    source = source or TypedCogValue(signature.input_type_ref, {})
    context = CogRunContext(snapshot.fingerprint, uuid4(), uuid4(), uuid4())
    binding = WorkBinding(
        "browser" if signature in BROWSER_COGS else "tool",
        "syn://thing/journal-profile-test", signature.input_type_ref,
        signature.output_type_ref, signature.binding_hash, "read_only", selected_policy,
    )
    return PinnedExecutionJournal(
        object(), object(), object(), snapshot_id=uuid4(), signature=signature,
        source=source, context=context, binding=binding, emitter=uuid4(),
    )


@pytest.mark.parametrize(("snapshot_factory", "allowed"), PROFILES)
def test_each_exact_profile_retains_only_its_installed_signatures(snapshot_factory, allowed):
    snapshot = snapshot_factory()
    for signature in allowed:
        created = journal(signature, snapshot)
        assert created.scope["catalog_hash"] == snapshot.fingerprint
        assert created.scope["cog_ref"] == str(signature.cog_ref)
        assert created.scope["binding_hash"] == signature.binding_hash


@pytest.mark.parametrize("signature", BROWSER_COGS)
@pytest.mark.parametrize("snapshot_factory", [row[0] for row in PROFILES[:3]])
def test_browser_cog_is_not_admitted_by_an_execution_or_artifact_snapshot(
    signature, snapshot_factory,
):
    with pytest.raises(ScopeDeniedError, match="exact installed Cog"):
        journal(signature, snapshot_factory())


@pytest.mark.parametrize("signature", (*RANGE_COGS, *UPLOAD_COGS))
def test_browser_snapshot_does_not_import_artifact_capabilities(signature):
    with pytest.raises(ScopeDeniedError, match="exact installed Cog"):
        journal(signature, browser_cog_snapshot())


@pytest.mark.parametrize("signature", BROWSER_COGS)
def test_browser_journal_rejects_changed_revision_and_input_port(signature):
    with pytest.raises(ScopeDeniedError):
        journal(replace(signature, implementation_revision=2), browser_cog_snapshot())
    with pytest.raises(ScopeDeniedError):
        journal(signature, browser_cog_snapshot(), source=TypedCogValue("syn://type/other", {}))
    with pytest.raises(ScopeDeniedError):
        journal(signature, SimpleNamespace(fingerprint="0" * 64))


def test_failed_operator_stop_still_revokes_local_service_dispatch():
    actor, _, apps, service = setup()
    invocation = uuid4()
    service.dispatch("act", request({"op": "navigate", "url": "about:blank"}), invocation, actor)

    def stop_failed(_session):
        raise OSError("operator unavailable; no process termination claim")

    apps.fence_browser = stop_failed
    with pytest.raises(OSError):
        service.fence_invocation(invocation)
    with pytest.raises(StateMismatchError, match="session is fenced"):
        service.dispatch("observe", request({"op": "state"}), uuid4(), actor)
    assert len(apps.commands) == 1


def test_fence_lookup_uses_only_admitted_invocation_ids():
    actor, _, apps, service = setup()
    service.fence_invocation(uuid4())
    assert apps.fenced == []
    with pytest.raises(ScopeDeniedError):
        service.fence_invocation(str(uuid4()))
    with pytest.raises(ScopeDeniedError):
        service.fence("b" * 32)
    service.dispatch("observe", request({"op": "state"}), uuid4(), actor)
    assert len(apps.commands) == 1


class _Catalog:
    def validate(self, value):
        Draft202012Validator(schemas()[value.type_ref]).validate(value.value)


class _Work:
    def __init__(self, signature, context, fail_at):
        self.signature, self.context = signature, context
        self.fail_at = fail_at
        self.input = {"id": uuid4()}
        self.terminal = False
        self.child_runs = []
        self.progresses = []
        self.failed = None

    def progress(self, phase):
        self.progresses.append(phase)
        if self.fail_at == phase:
            raise OSError("injected progress persistence failure")

    def complete(self, outcome):
        if self.fail_at in {"completion", "completion-and-failure"}:
            raise OSError("injected completion persistence failure")
        self.terminal = True
        self.outcome = outcome

    def fail(self, **status):
        if self.fail_at == "completion-and-failure":
            raise OSError("injected terminal persistence failure")
        self.failed = status
        self.terminal = True


def runtime_fixture(monkeypatch, *, fail_at=None, deny_at=None):
    actor, storage, apps, service = setup()
    host = ExecutionCogRuntime.__new__(ExecutionCogRuntime)
    snapshot = browser_cog_snapshot()
    calls, work = [], []

    def admit(*_args):
        calls.append(len(calls) + 1)
        if len(calls) == deny_at:
            raise ScopeDeniedError("injected policy revocation")

    class Monitor:
        def __init__(self, *_args, **_kwargs):
            pass

        def begin(self, signature, _source, context, **_kwargs):
            created = _Work(signature, context, fail_at)
            work.append(created)
            return created

        def denied(self, *_args):
            pass

    monkeypatch.setattr(execution_cogs, "CogWorkMonitor", Monitor)
    host.__dict__.update(
        db=object(), artifacts=storage, actor=actor, snapshot_id=uuid4(),
        task_revision=uuid4(), repository_state=uuid4(), emitter=uuid4(),
        policy=service.policy, admit=admit, receipts=None, catalog=_Catalog(),
        registry=SimpleNamespace(snapshot=snapshot), operations={},
        _direct_operations=installed_operations(service, service.policy),
        sandbox_executor=None, sandbox_agent_id=None, browser_service=service,
        _seen=set(), _live={}, _poisoned=False,
    )
    return SimpleNamespace(host=host, actor=actor, storage=storage, apps=apps,
                           service=service, calls=calls, work=work)


def invocation(fixture):
    return CogRunContext(
        fixture.host.registry.snapshot.fingerprint, fixture.host.task_revision,
        fixture.host.repository_state, uuid4(),
    )


def command(signature):
    if signature == ACT:
        return {"op": "role.click", "role": "button", "name": "Submit"}
    if signature == PICK:
        return {"op": "inspect.start"}
    if signature == CAPTURE:
        return {"op": "sample"}
    return {"op": "state"}


def invoke(fixture, signature=ACT, context=None):
    return asyncio.run(fixture.host.invoke(
        signature, TypedCogValue(signature.input_type_ref, request(command(signature))),
        context or invocation(fixture),
    ))


@pytest.mark.parametrize("signature", [ACT, PICK])
@pytest.mark.parametrize("fail_at", [
    "browser_evidence_persisted", "typed_result_validated", "completion", "completion-and-failure",
])
def test_post_effect_journal_failures_fence_and_do_not_retry(monkeypatch, signature, fail_at):
    fixture = runtime_fixture(monkeypatch, fail_at=fail_at)
    with pytest.raises((OSError, IndeterminateExecutionError)):
        invoke(fixture, signature)
    assert len(fixture.apps.commands) == 1
    assert SESSION in fixture.apps.fenced
    assert fixture.host._poisoned is True
    if fail_at != "completion-and-failure":
        assert fixture.work[-1].failed["uncertain"] is True
    with pytest.raises(ScopeDeniedError):
        invoke(fixture, signature)
    # A fresh runtime cannot reuse this service's capability after fencing.
    with pytest.raises(StateMismatchError, match="session is fenced"):
        fixture.service.dispatch("observe", request({"op": "state"}), uuid4(), fixture.actor)
    assert len(fixture.apps.commands) == 1


@pytest.mark.parametrize("signature", [ACT, PICK])
def test_revocation_after_dispatch_fences_the_action(monkeypatch, signature):
    fixture = runtime_fixture(monkeypatch, deny_at=4)
    with pytest.raises(ScopeDeniedError, match="policy revocation"):
        invoke(fixture, signature)
    assert len(fixture.apps.commands) == 1
    assert SESSION in fixture.apps.fenced
    assert fixture.work[-1].failed["uncertain"] is True


@pytest.mark.parametrize("deny_at", [1, 2, 3])
def test_denied_before_dispatch_does_not_fence_an_uninvoked_session(monkeypatch, deny_at):
    fixture = runtime_fixture(monkeypatch, deny_at=deny_at)
    with pytest.raises(ScopeDeniedError):
        invoke(fixture)
    assert fixture.apps.commands == []
    assert fixture.apps.fenced == []


def test_output_validation_failure_after_dispatch_is_fenced(monkeypatch):
    fixture = runtime_fixture(monkeypatch)
    validate = fixture.host.catalog.validate

    def reject_result(value):
        if value.type_ref == EVIDENCE_RESULT:
            raise StateMismatchError("injected result rejection")
        validate(value)

    fixture.host.catalog.validate = reject_result
    with pytest.raises(StateMismatchError, match="result rejection"):
        invoke(fixture)
    assert SESSION in fixture.apps.fenced
    assert len(fixture.apps.commands) == 1


def test_fence_failure_does_not_mask_the_original_journal_error(monkeypatch):
    fixture = runtime_fixture(monkeypatch, fail_at="completion")

    def stop_failed(_session):
        raise RuntimeError("injected stop failure")

    fixture.apps.fence_browser = stop_failed
    with pytest.raises(OSError, match="completion persistence"):
        invoke(fixture)
    assert fixture.host._poisoned
    with pytest.raises(StateMismatchError, match="session is fenced"):
        fixture.service.dispatch("observe", request({"op": "state"}), uuid4(), fixture.actor)


def test_success_and_read_only_persistence_failure_do_not_fence(monkeypatch):
    fixture = runtime_fixture(monkeypatch)
    result = invoke(fixture)
    assert result.value.value["status"] == "recorded"
    assert fixture.apps.fenced == [] and not fixture.host._poisoned
    fixture = runtime_fixture(monkeypatch, fail_at="completion")
    with pytest.raises(OSError):
        invoke(fixture, OBSERVE)
    assert fixture.apps.fenced == [] and not fixture.host._poisoned
    assert fixture.work[-1].failed["uncertain"] is False


def test_cancelled_rpc_cannot_reopen_the_service(monkeypatch):
    fixture = runtime_fixture(monkeypatch)
    started, release = threading.Event(), threading.Event()
    original_fence = fixture.apps.fence_browser

    def blocking_command(session, selected):
        fixture.apps.commands.append((session, selected))
        started.set()
        if not release.wait(3):
            raise TimeoutError("test RPC never received its release")
        return {"acted": True}

    def stop(session):
        original_fence(session)
        release.set()

    fixture.apps.browser_command, fixture.apps.fence_browser = blocking_command, stop

    async def exercise():
        task = asyncio.create_task(fixture.host.invoke(
            ACT, TypedCogValue(ACT.input_type_ref, request(command(ACT))), invocation(fixture),
        ))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises((asyncio.CancelledError, IndeterminateExecutionError)):
                await task
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
    assert SESSION in fixture.apps.fenced
    assert len(fixture.apps.commands) == 1
    with pytest.raises(StateMismatchError, match="session is fenced"):
        fixture.service.dispatch("observe", request({"op": "state"}), uuid4(), fixture.actor)
