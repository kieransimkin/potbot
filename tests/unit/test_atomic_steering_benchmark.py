"""The atomic/steering benchmark holds back reference answers from the prompt."""

import json
from pathlib import Path

from potbot.evaluation.agentic_model_bench import RoleSuite, prompt_for_case, rubric_score
from potbot.models.prompt_compiler import profile_for_model


def test_steering_atomicity_cases_are_disjoint_holdouts() -> None:
    path = Path(__file__).resolve().parents[2] / "examples/agentic-benchmark/atomic-steering-suite.json"
    suite = RoleSuite.from_dict(json.loads(path.read_text(encoding="utf-8")))
    assert len(suite.cases) >= 12
    assert len({c.id for c in suite.cases}) == len(suite.cases)
    assert {"steering_timing", "status", "atomicity", "tool_selection", "assumption"} <= {
        case.role for case in suite.cases
    }
    assert all(case.split == "holdout" for case in suite.cases)
    for case in suite.cases:
        prompt = prompt_for_case("Qwen/Qwen3-0.6B", case, "model_role")
        assert case.expected != {}
        assert json.dumps(case.expected) not in prompt.user
        assert rubric_score(case, json.dumps(case.expected)) == 1


def test_specialized_prompt_family_controls_never_claim_tools() -> None:
    assert profile_for_model("google/functiongemma-270m-it").family == "functiongemma"
    assert profile_for_model("ibm-granite/granite-4.0-h-tiny").family == "granite4"
    assert profile_for_model("HuggingFaceTB/SmolLM2-360M-Instruct").family == "smollm"
