"""Evidence-guided recursive expansion obeys parentage and execution bounds."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from potbot.controller.reasoning_investigation import ReasoningInvestigator
from potbot.controller.reasoning_tree import ReasoningTree
from potbot.domain.auth import WorkspaceContext


def _hypothesis(name: str) -> str:
    return json.dumps({"hypotheses": [
        {"hypothesis": name, "assumptions": [], "questions": []}
    ]})


def _critique(prompt: str) -> str:
    packet = json.loads(prompt.split("JSON data):\n", 1)[1])
    return json.dumps({"evaluations": [
        {"node_id": branch["node_id"], "relevance": 4,
         "evidence_alignment": 1, "falsification_risk": 1,
         "uncertainty": 1, "recommendation": "keep",
         "rationale": "This hypothesis needs a discriminating test",
         "issues": [], "tests": []}
        for branch in packet["context"]["branches"]
    ]})


def test_expansion_is_bounded_and_retains_parent_node(tmp_path: Path) -> None:
    context = WorkspaceContext(uuid4(), uuid4())
    generator_calls = []
    rounds = []

    def model(model, system, prompt, options, on_text, run_id, cancelled):
        packet = json.loads(prompt.split("JSON data):\n", 1)[1])
        if "'hypotheses'" in system:
            round_index = packet["context"].get("round", 0)
            generator_calls.append(round_index)
            text = _hypothesis("Refine with a second test") if round_index else _hypothesis("Initial hypothesis")
        elif "'evaluations'" in system:
            text = _critique(prompt)
        else:
            text = "Evidence is insufficient for final verification"
        return text, {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}, "stop"

    runner = ReasoningInvestigator(root=tmp_path, max_rounds=2)
    result = runner.run(
        context=context, run_id=uuid4().hex, task="Find causes", system="",
        models=[{"provider": "openai", "model": "test-model"}],
        options={"max_output_tokens": 512}, invoke=model,
        on_text=lambda text: None,
        emit=lambda event, **fields: rounds.append(event),
    )
    assert 1 in generator_calls
    tree = ReasoningTree.load(result.tree_path)
    investigations = [node for node in tree.nodes.values() if node.role == "investigator"]
    assert len(investigations) == 2
    assert any(node.parent_id in {n.id for n in investigations} for node in investigations)
    assert "reasoning.branches.expanded" in rounds
    assert result.invocations <= 8
