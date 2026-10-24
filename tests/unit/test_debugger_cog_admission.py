"""Denied, revoked or unmonitored Cog operations never reach an adapter."""

import asyncio
import hashlib
import json
from dataclasses import asdict, replace
from uuid import uuid4

import pytest

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.cogs.debugger import cog_ref, signature
from potbot.cogs.registry import BoundCogRegistry
from potbot.controller.cog_work import CogWorkMonitor
from potbot.debugging import DebugIdentity, DebugSession
from potbot.debugging.cog_runtime import DebuggerCogRuntime
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError, StateMismatchError


@pytest.mark.parametrize("case", ["deny", "revoked", "monitor_failure", "mutated", "wrong_type"])
def test_cog_admission_and_work_storage_fail_closed(tmp_path, case):
    class Adapter:
        on_event = None

        def __init__(self):
            self.calls = []

        async def call(self, operation, arguments):
            self.calls.append(operation)
            return {"operations": []}

        async def close(self):
            pass

    class Events:
        def __init__(self):
            self.values = []

        def append(self, actor, event):
            if case == "monitor_failure" and event.stage == "updated":
                raise OSError("authored storage fault")
            identifier = uuid4()
            value = {
                **asdict(event),
                "id": str(identifier),
                "event_ref": f"syn://event/{identifier}",
                "telemetry": json.loads(event.telemetry_json),
            }
            self.values.append(value)
            return value

    class Artifacts:
        def put(self, actor, data, media_type):
            return {"id": uuid4(), "digest": hashlib.sha256(data).hexdigest()}

    program = tmp_path / "fixture.py"
    program.write_text("pass\n", encoding="utf-8")
    actor = WorkspaceContext(uuid4(), uuid4())
    identity = DebugIdentity(
        actor.workspace_id,
        uuid4(),
        tmp_path,
        (("fixture.py", hashlib.sha256(program.read_bytes()).hexdigest()),),
        "fixture",
        "1",
        "fixture",
        "synny://type/workflow-execution",
    )
    registry, adapter, events = BoundCogRegistry.debugger(), Adapter(), Events()
    admissions = []

    def admit(selected, source, context):
        admissions.append(context.invocation_id)
        if case == "deny" or case == "revoked" and len(admissions) == 2:
            raise ScopeDeniedError("Authored denied/revoked fixture")
        if case == "mutated" and len(admissions) == 2:
            source.value["schema_version"] = 2

    async def scenario():
        session = DebugSession(adapter, identity, admit=lambda *_: None, record=lambda _: None)
        runtime = DebuggerCogRuntime(
            session,
            CogWorkMonitor(
                events,
                Artifacts(),
                actor,
                emitter_run_id=identity.emitter_run_id,
                event_type_ref=identity.event_type_ref,
            ),
            task_revision_id=uuid4(),
            repository_state_id=uuid4(),
            catalog_hash=registry.snapshot.fingerprint,
            admit=admit,
        )
        request = TypedCogValue(
            signature("stack" if case == "wrong_type" else "initialize").input_type_ref,
            {"schema_version": 1},
        )
        context = CogRunContext(
            registry.snapshot.fingerprint, runtime.task_revision, runtime.repository_state, uuid4()
        )
        expected = (
            OSError
            if case == "monitor_failure"
            else StateMismatchError
            if case in {"mutated", "wrong_type"}
            else ScopeDeniedError
        )
        with pytest.raises(expected):
            await registry.invoke(cog_ref("initialize"), request, context, controller=runtime)
        assert adapter.calls == []
        assert not any(e["stage"] == "completed" for e in events.values)
        if case == "deny":
            assert [e["stage"] for e in events.values] == ["failed"]
            assert events.values[0]["telemetry"]["phase"] == "admission_denied"
        if case == "monitor_failure":
            assert events.values[-1]["stage"] == "indeterminate"
            with pytest.raises(ScopeDeniedError):
                await registry.invoke(
                    cog_ref("initialize"),
                    request,
                    replace(context, invocation_id=uuid4()),
                    controller=runtime,
                )

    asyncio.run(scenario())
