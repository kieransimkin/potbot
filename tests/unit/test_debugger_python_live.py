"""Real Python/debugpy, explicit authored fixture; NOT sandbox qualification."""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "debug_python_demo", Path(__file__).resolve().parents[2] / "scripts/debug_python_demo.py"
)
DEMO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEMO)


@pytest.mark.parametrize("fail", [False, True])
def test_real_python_breakpoint_steps_stack_and_exception(tmp_path, fail):
    import asyncio

    result = asyncio.run(DEMO.demonstrate(tmp_path, fail=fail))
    assert result["result"] == "passed" and result["adapter_exited"]
    by_action = {x["action"]: x for x in result["observations"]}
    assert {"breakpoint", "step_over", "step_into", "step_out", "expired_frame_rejected"} <= set(
        by_action
    )
    entered = by_action["step_into"]["frames"]
    assert entered[0]["function"] == "leaf" and entered[0]["locals"]["value"] == "10"
    assert entered[1]["function"] == "outer" and entered[1]["locals"]["seed"] == "5"
    if fail:
        assert by_action["exception"]["reason"] == "exception"
    else:
        assert by_action["natural_exit"]["exit_code"] == 0
    assert result["execution_events"] and all(
        e["trigger_input_sha256"] for e in result["execution_events"]
    )
