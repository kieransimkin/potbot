"""Finite, operator-installed child plans. Mock-only, not a kernel qualification."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.controller.child_plans import (
    ChildWorkStep,
    InstalledChildPlan,
    compile_child_plans,
    execute_child_plan,
)
from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.tools.execution_contracts import ExecutionPolicy, WorkBinding
from potbot.tools.sandbox_cog_worker import SandboxedCogOperation

POLICY = ExecutionPolicy("a" * 64, "b" * 64, "c" * 64, "test")


def operation(kind, name, *, effect="read_only", input_type="syn://type/in"):
    return SandboxedCogOperation(
        WorkBinding(
            kind,
            "syn://thing/" + name,
            input_type,
            "syn://type/out",
            "d" * 64,
            effect,
            POLICY,
        ),
        ("/usr/bin/potbot-cog-worker",),
        "e" * 64,
    )


def example():
    root = operation("workflow", "root")
    script = operation("script", "script")
    server = operation("mcp_tool", "mcp")
    child = operation("tool", "child")
    plan = InstalledChildPlan(
        "workflow",
        "syn://thing/root",
        (
            ChildWorkStep("script", "script", "syn://thing/script"),
            ChildWorkStep("mcp", "mcp_tool", "syn://thing/mcp", links=("script",)),
            ChildWorkStep("tool", "tool", "syn://thing/child"),
        ),
    )
    return (root, script, server, child), plan


def test_installs_plan_for_exact_pinned_operations_and_keeps_immutable_mapping():
    operations, plan = example()
    compiled = compile_child_plans(operations, (plan,))
    assert compiled[plan.key] == plan
    with pytest.raises(TypeError):
        compiled[plan.key] = plan
    assert compile_child_plans(operations, ()) == {}


@pytest.mark.parametrize(
    "change",
    [
        lambda o, p: (o[1:], (p,)),
        lambda o, p: (o, (p, p)),
        lambda o, p: (o, (replace(p, parent_operation_ref="syn://thing/unknown"),)),
        lambda o, p: (
            o,
            (replace(p, steps=(ChildWorkStep("bad", "tool", "syn://thing/missing"),)),),
        ),
        lambda o, p: (
            (o[0], o[1], o[2], operation("tool", "child", input_type="syn://type/other")),
            (p,),
        ),
        lambda o, p: (
            (o[0], o[1], o[2], operation("tool", "child", effect="non_idempotent_write")),
            (p,),
        ),
    ],
)
def test_rejects_unknown_and_escalating_plans(change):
    ops, plan = example()
    with pytest.raises(ScopeDeniedError):
        compile_child_plans(*change(ops, plan))


def test_rejects_duplicate_forward_or_unknown_causation():
    for steps in (
        (
            ChildWorkStep("a", "tool", "syn://thing/child"),
            ChildWorkStep("a", "tool", "syn://thing/child"),
        ),
        (
            ChildWorkStep("a", "tool", "syn://thing/child", links=("b",)),
            ChildWorkStep("b", "tool", "syn://thing/child"),
        ),
        (ChildWorkStep("a", "tool", "syn://thing/child", links=("nope",)),),
    ):
        with pytest.raises(ScopeDeniedError):
            InstalledChildPlan("workflow", "syn://thing/root", steps)


def test_rejects_mutual_recursive_child_plans():
    first, second = operation("workflow", "first"), operation("workflow", "second")
    a = InstalledChildPlan(
        "workflow",
        first.binding.operation_ref,
        (ChildWorkStep("other", "workflow", second.binding.operation_ref),),
    )
    b = InstalledChildPlan(
        "workflow",
        second.binding.operation_ref,
        (ChildWorkStep("back", "workflow", first.binding.operation_ref),),
    )
    with pytest.raises(ScopeDeniedError, match="cycle"):
        compile_child_plans((first, second), (a, b))


def test_requires_same_policy_and_prevents_plan_submission_as_untrusted_data():
    op, plan = example()
    changed_policy = replace(POLICY, policy_revision_hash="f" * 64)
    altered = SandboxedCogOperation(
        replace(op[1].binding, policy=changed_policy), op[1].argv, op[1].executable_sha256
    )
    with pytest.raises(ScopeDeniedError):
        compile_child_plans((op[0], altered, op[2], op[3]), (plan,))
    with pytest.raises(ScopeDeniedError):
        compile_child_plans(op, ({"parent_kind": "workflow", "steps": []},))


def test_execution_links_only_a_durable_completed_child(monkeypatch):
    from potbot.controller import execution_events

    operations, plan = example()
    compile_child_plans(operations, (plan,))
    invocations = []
    first_run = uuid4()
    terminal = uuid4()

    class FakeEvents:
        def __init__(self, database):
            assert database == "db"

        def events(self, actor, **kwargs):
            assert actor == "actor" and kwargs["run_id"] == first_run
            return {
                "items": [{"id": str(terminal), "run_id": str(first_run), "stage": "completed"}],
                "has_more": False,
            }

    monkeypatch.setattr(execution_events, "ExecutionEventService", FakeEvents)

    class FakeCall:
        runtime = SimpleNamespace(db="db", actor="actor")

        async def child(self, kind, operation_ref, value, *, links=()):
            invocations.append((kind, operation_ref, value, links))
            return SimpleNamespace(receipt=SimpleNamespace(invocation_id=first_run))

    asyncio.run(execute_child_plan(FakeCall(), "fixture-input", plan))
    assert len(invocations) == 3
    assert invocations[1][3] == (terminal,)
    assert invocations[0][3] == invocations[2][3] == ()


def test_missing_terminal_child_never_dispatches_dependent_work(monkeypatch):
    from potbot.controller import execution_events

    _, plan = example()
    run = uuid4()

    class FakeEvents:
        def __init__(self, database):
            pass

        def events(self, actor, **kwargs):
            return {
                "items": [{"id": str(uuid4()), "run_id": str(run), "stage": "indeterminate"}],
                "has_more": False,
            }

    monkeypatch.setattr(execution_events, "ExecutionEventService", FakeEvents)
    recorded = []

    class FakeCall:
        runtime = SimpleNamespace(db="db", actor="actor")

        async def child(self, *args, **kwargs):
            recorded.append(args)
            return SimpleNamespace(receipt=SimpleNamespace(invocation_id=run))

    with pytest.raises(StateMismatchError):
        asyncio.run(execute_child_plan(FakeCall(), "fixture-input", plan))
    assert len(recorded) == 1
