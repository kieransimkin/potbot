"""Shared debugger contracts, independent of language/backend or host process launch."""

import asyncio
import hashlib
from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.debugging import DebugIdentity, DebugSession, DebugState
from potbot.domain.errors import (
    CapabilityUnavailableError,
    IndeterminateExecutionError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.tools.secrets import CaptureRedactor


class Adapter:
    on_event = None

    def __init__(self):
        self.calls = []
        self.closed = False
        self.before_response = None
        self.value = "11"

    async def call(self, operation, args):
        self.calls.append((operation, args))
        if self.before_response is not None:
            self.before_response()
        return {
            "initialize": {
                "operations": [
                    "stack",
                    "scopes",
                    "variables",
                    "step_over",
                    "disconnect",
                    "evaluate",
                ]
            },
            "stack": {
                "frames": [{"id": 1, "name": "inner", "source": None, "line": 2, "column": 1}]
            },
            "scopes": {"scopes": [{"name": "Locals", "reference": 2}]},
            "variables": {
                "variables": [
                    {"name": "result", "value": self.value, "reference": 0},
                    {"name": "access_token", "value": "never-return-me", "reference": 3},
                ]
            },
        }.get(operation, {})

    async def begin(self, operation, args):
        return 1

    async def finish(self, request):
        return {}

    async def close(self):
        self.closed = True


def identity(tmp_path):
    source = tmp_path / "target.py"
    source.write_text("result = 11\n", encoding="utf-8")
    return DebugIdentity(
        uuid4(),
        uuid4(),
        tmp_path,
        (("target.py", hashlib.sha256(source.read_bytes()).hexdigest()),),
        "language-neutral-test-adapter",
        "1",
        "test-runtime",
        "synny://type/workflow-execution",
    )


def stop(adapter):
    adapter.on_event(
        "stopped", {"thread_id": 1, "reason": "breakpoint", "all_threads_stopped": True}
    )


def test_stack_scopes_variables_and_stale_frame_rejection(tmp_path):
    async def scenario():
        adapter = Adapter()
        events = []
        session = DebugSession(
            adapter, identity(tmp_path), admit=lambda *_: None, record=events.append
        )
        await session.initialize()
        stop(adapter)
        current = await session.wait_stopped()
        frame = (await session.stack(current))[0]
        scope = (await session.scopes(frame))[0]
        values = await session.variables(scope.reference)
        assert values[0].value == "11"
        assert values[1].value == "[REDACTED]" and values[1].reference is None
        assert "never-return-me" not in repr(events)
        await session.resume(current, operation="step_over")
        with pytest.raises(StateMismatchError):
            await session.scopes(frame)
        stop(adapter)
        with pytest.raises(StateMismatchError):
            await session.variables(scope.reference)
        assert (await session.wait_stopped()).revision > current.revision
        assert any(e.stage == "completed" for e in events)
        await session.close()

    asyncio.run(scenario())


def test_denied_step_preserves_confirmed_stop_without_dispatch(tmp_path):
    def admit(operation, _):
        if operation == "debug.step_over":
            raise ScopeDeniedError("No control grant")

    async def scenario():
        adapter = Adapter()
        session = DebugSession(adapter, identity(tmp_path), admit=admit, record=lambda _: None)
        await session.initialize()
        stop(adapter)
        current = session.stop
        with pytest.raises(ScopeDeniedError):
            await session.resume(current, operation="step_over")
        assert session.stop == current and session.state == DebugState.STOPPED
        assert not any(op == "step_over" for op, _ in adapter.calls)
        await session.close()

    asyncio.run(scenario())


def test_other_session_and_forged_frame_rejected(tmp_path):
    async def scenario():
        adapter = Adapter()
        session = DebugSession(
            adapter, identity(tmp_path), admit=lambda *_: None, record=lambda _: None
        )
        await session.initialize()
        stop(adapter)
        frame = (await session.stack(session.stop))[0]
        with pytest.raises(StateMismatchError):
            await session.scopes(replace(frame, stop=replace(frame.stop, session_id=uuid4())))
        with pytest.raises(ScopeDeniedError):
            await session.scopes(replace(frame, adapter_id=12345))
        await session.close()

    asyncio.run(scenario())


def test_continued_during_inspection_discards_the_result(tmp_path):
    async def scenario():
        adapter = Adapter()
        session = DebugSession(
            adapter, identity(tmp_path), admit=lambda *_: None, record=lambda _: None
        )
        await session.initialize()
        stop(adapter)
        current = session.stop
        adapter.before_response = lambda: adapter.on_event("continued", {})
        with pytest.raises(StateMismatchError):
            await session.stack(current)
        assert session.state == DebugState.RUNNING
        adapter.before_response = None
        await session.close()

    asyncio.run(scenario())


def test_expression_execution_denied_by_default_and_source_change_blocks_control(tmp_path):
    async def scenario():
        adapter = Adapter()
        session = DebugSession(
            adapter, identity(tmp_path), admit=lambda *_: None, record=lambda _: None
        )
        await session.initialize()
        stop(adapter)
        frame = (await session.stack(session.stop))[0]
        with pytest.raises(ScopeDeniedError):
            await session.evaluate(frame, "delete_everything()")
        (tmp_path / "target.py").write_text("changed\n", encoding="utf-8")
        with pytest.raises(StateMismatchError):
            await session.resume(frame.stop)
        assert not any(op == "evaluate" for op, _ in adapter.calls)
        await session.close()

    asyncio.run(scenario())


def test_missing_backend_operation_and_stop_timeout_are_explicit(tmp_path):
    async def scenario():
        adapter = Adapter()
        session = DebugSession(
            adapter,
            identity(tmp_path),
            admit=lambda *_: None,
            record=lambda _: None,
            timeout_s=0.01,
        )
        await session.initialize()
        stop(adapter)
        with pytest.raises(CapabilityUnavailableError):
            await session.resume(session.stop, operation="step_into")
        assert session.state == DebugState.STOPPED
        adapter.on_event("continued", {})
        with pytest.raises(IndeterminateExecutionError):
            await session.wait_stopped()
        assert session.state == DebugState.INDETERMINATE
        await session.close()

    asyncio.run(scenario())


def test_artifacts_and_output_redaction_use_existing_event_contract(tmp_path):
    async def scenario():
        adapter = Adapter()
        captured = []
        events = []
        session = DebugSession(
            adapter,
            identity(tmp_path),
            admit=lambda *_: None,
            record=events.append,
            capture_output=lambda x: captured.append(x) or "artifact:fixture",
            redactor=CaptureRedactor(("literal-secret",)),
        )
        await session.initialize()
        stop(adapter)
        frame = (await session.stack(session.stop))[0]
        scope = (await session.scopes(frame))[0]
        adapter.value = "literal-secret"
        values = await session.variables(scope.reference)
        assert values[0].value == "[REDACTED]"
        assert "literal-secret" not in repr(captured) and "never-return-me" not in repr(captured)
        assert all(e.output_artifact_ref is not None for e in events if e.output_sha256)
        await session.close()

    asyncio.run(scenario())


def test_outside_inventory_and_no_admission_are_denied(tmp_path):
    selected = identity(tmp_path)
    with pytest.raises(ScopeDeniedError):
        selected.source("../elsewhere.py")
    with pytest.raises(ScopeDeniedError):
        selected.source("unselected.py")

    async def scenario():
        with pytest.raises(ScopeDeniedError):
            DebugSession(Adapter(), selected, admit=None, record=lambda _: None)

    asyncio.run(scenario())
