"""Reusable real-backend acceptance driver; does not launch any process."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from potbot.debugging.python import launch_configuration
from potbot.debugging.session import DebugSession
from potbot.domain.errors import StateMismatchError


async def python_walkthrough(
    session: DebugSession,
    *,
    program: Path,
    python: Path,
    fail: bool = False,
) -> list[dict[str, Any]]:
    """Break, inspect two frames, step over/into/over/out, and continue."""
    source = program.read_text(encoding="utf-8").splitlines()
    lines = {
        marker: next(i for i, text in enumerate(source, 1) if f"# {marker}" in text)
        for marker in ("OUTER_START", "OUTER_CALL", "LEAF_VALUE", "LEAF_RETURN", "OUTER_RETURN")
    }
    observations: list[dict[str, Any]] = []
    config = launch_configuration(
        session.identity, program=program, python=python, arguments=("--fail",) if fail else ()
    )
    await session.launch(
        config,
        breakpoints={program: (lines["OUTER_START"],)},
        exception_filters=("uncaught",) if fail else (),
    )
    stop = await session.wait_stopped()

    async def inspect(label: str, expected: int) -> None:
        stack = await session.stack(stop)
        assert len(stack) >= 2 and stack[0].line == expected
        values = []
        for frame in stack[:2]:
            scopes = await session.scopes(frame)
            local = next(s for s in scopes if s.name == "Locals")
            variables = await session.variables(local.reference)
            values.append(
                {
                    "function": frame.name,
                    "line": frame.line,
                    "locals": {v.name: v.value for v in variables if not v.name.startswith("_")},
                }
            )
        observations.append(
            {"action": label, "revision": stop.revision, "reason": stop.reason, "frames": values}
        )

    await inspect("breakpoint", lines["OUTER_START"])
    old_frames = await session.stack(stop)
    stop = await session.step(stop, "step_over")
    try:
        await session.scopes(old_frames[0])
    except StateMismatchError:
        observations.append({"action": "expired_frame_rejected", "revision": stop.revision})
    else:
        raise AssertionError("Stale frame was accepted")
    await inspect("step_over", lines["OUTER_CALL"])
    stop = await session.step(stop, "step_into")
    await inspect("step_into", lines["LEAF_VALUE"])
    stop = await session.step(stop, "step_over")
    await inspect("step_over_leaf", lines["LEAF_RETURN"])
    stop = await session.step(stop, "step_out")
    returned = await session.stack(stop)
    assert returned[0].name == "outer"
    assert returned[0].line in {lines["OUTER_CALL"], lines["OUTER_RETURN"]}
    await inspect("step_out", returned[0].line)
    if returned[0].line == lines["OUTER_CALL"]:
        stop = await session.step(stop, "step_over")
        await inspect("caller_assignment", lines["OUTER_RETURN"])
    await session.resume(stop)
    if fail:
        stop = await session.wait_stopped(after_revision=stop.revision)
        assert stop.reason == "exception"
        stack = await session.stack(stop)
        observations.append(
            {
                "action": "exception",
                "revision": stop.revision,
                "reason": stop.reason,
                "function": stack[0].name,
                "line": stack[0].line,
                "details": await session.exception_info(stop),
            }
        )
    else:
        assert await session.wait_terminated() == 0
        observations.append({"action": "natural_exit", "exit_code": session.exit_code})
    return observations
