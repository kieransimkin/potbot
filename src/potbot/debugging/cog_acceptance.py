"""Real Python breakpoint/step/stack acceptance driven exclusively through SYN Cogs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from potbot.debugging.cog_client import DebuggerCogClient
from potbot.domain.errors import StateMismatchError


async def python_cog_walkthrough(
    client: DebuggerCogClient,
    *,
    program: Path,
    configuration: dict[str, Any],
    fail: bool = False,
    attached: bool = False,
) -> list[dict[str, Any]]:
    source = program.read_text(encoding="utf-8").splitlines()
    markers = {
        name: next(i for i, line in enumerate(source, 1) if f"# {name}" in line)
        for name in ("OUTER_START", "OUTER_CALL", "LEAF_VALUE", "LEAF_RETURN", "OUTER_RETURN")
    }
    observations: list[dict[str, Any]] = []
    await client.call("initialize")
    await client.call("attach" if attached else "launch", configuration=configuration)
    breakpoints = await client.call(
        "set_breakpoints", source=str(program), lines=[markers["OUTER_START"]]
    )
    assert breakpoints["breakpoints"][0]["verified"] is True
    await client.call("configure_exceptions", filters=["uncaught"] if fail else [])
    await client.call("configuration_done")
    stop = await client.call("wait_stopped", after_revision=0)
    threads = await client.call("threads")
    assert stop["thread_id"] in {t["id"] for t in threads["threads"]}

    async def inspect(action: str, expected: int | tuple[int, ...]) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = (await client.call("stack", stop_ref=stop["stop_ref"]))[
            "frames"
        ]
        assert len(frames) >= 2
        assert frames[0]["line"] in ((expected,) if isinstance(expected, int) else expected)
        views = []
        for frame in frames[:2]:
            scopes = (await client.call("scopes", frame_ref=frame["frame_ref"]))["scopes"]
            local = next(s for s in scopes if s["name"] == "Locals")
            values = (await client.call("variables", value_ref=local["value_ref"]))["variables"]
            views.append(
                {
                    "function": frame["name"],
                    "line": frame["line"],
                    "locals": {
                        v["name"]: v["value"] for v in values if not v["name"].startswith("_")
                    },
                }
            )
        observations.append(
            {
                "action": action,
                "stop_revision": stop["revision"],
                "reason": stop["reason"],
                "frames": views,
            }
        )
        return frames

    frames = await inspect("breakpoint", markers["OUTER_START"])
    old_frame = frames[0]["frame_ref"]
    stop = await client.call("step_over", stop_ref=stop["stop_ref"])
    try:
        await client.call("scopes", frame_ref=old_frame)
    except StateMismatchError:
        observations.append({"action": "expired_frame_rejected"})
    else:
        raise AssertionError("Expired Cog frame handle was accepted")
    await inspect("step_over", markers["OUTER_CALL"])
    stop = await client.call("step_into", stop_ref=stop["stop_ref"])
    await inspect("step_into", markers["LEAF_VALUE"])
    stop = await client.call("step_over", stop_ref=stop["stop_ref"])
    await inspect("step_over_leaf", markers["LEAF_RETURN"])
    stop = await client.call("step_out", stop_ref=stop["stop_ref"])
    frames = await inspect("step_out", (markers["OUTER_CALL"], markers["OUTER_RETURN"]))
    assert frames[0]["name"] == "outer"
    if frames[0]["line"] == markers["OUTER_CALL"]:
        stop = await client.call("step_over", stop_ref=stop["stop_ref"])
        await inspect("caller_assignment", markers["OUTER_RETURN"])
    await client.call("set_breakpoints", source=str(program), lines=[])
    revision = stop["revision"]
    await client.call("continue", stop_ref=stop["stop_ref"])
    if attached:
        await client.call("detach")
        observations.append({"action": "detached"})
    elif fail:
        stop = await client.call("wait_stopped", after_revision=revision)
        assert stop["reason"] == "exception"
        frames = (await client.call("stack", stop_ref=stop["stop_ref"]))["frames"]
        details = await client.call("exception_info", stop_ref=stop["stop_ref"])
        observations.append({"action": "exception", "frames": frames, "details": details})
        await client.call("terminate")
    else:
        result = await client.call("wait_terminated")
        assert result["exit_code"] == 0
        observations.append({"action": "natural_exit", "exit_code": 0})
        await client.call("terminate")
    assert all(out.receipt.cog_ref.startswith("syn://cog/potbot-debug-") for out in client.history)
    return observations
