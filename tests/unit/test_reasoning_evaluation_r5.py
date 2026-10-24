"""R5 independent critique, evidence scoring, beam selection and provenance."""

from __future__ import annotations

import json

import pytest

from potbot.controller.reasoning_evaluation import parse_critiques, rank_branches
from potbot.domain.errors import InvalidInputError


def _review(ids: list[str]) -> str:
    return json.dumps({"evaluations": [
        {"node_id": node_id, "relevance": 3, "evidence_alignment": 2,
         "falsification_risk": 1, "uncertainty": 2, "recommendation": "keep",
         "rationale": "Needs independent verification", "issues": [], "tests": []}
        for node_id in ids
    ]})


def test_rejects_missing_unknown_duplicate_or_invented_scores() -> None:
    assert len(parse_critiques(_review(["a", "b"]), {"a", "b"})) == 2
    for bad in (_review(["a", "a"]), _review(["a", "unknown"]), _review(["a"]),
                _review(["a", "b"]).replace('"relevance": 3', '"relevance": 100'),
                _review(["a", "b"]).replace('"rationale"', '"shell_command"')):
        with pytest.raises(InvalidInputError):
            parse_critiques(bad, {"a", "b"})


def test_source_receipts_improve_rank_but_do_not_fabricate_evidence() -> None:
    critiques = parse_critiques(_review(["a", "b", "c"]), {"a", "b", "c"})
    branches = [
        {"node_id": "a", "hypothesis": "Evidence-backed", "investigation": [
            {"channel": "external_tools", "status": "completed", "findings": [
                {"source_ref": "syn://source/a", "source_sha256": "a" * 64,
                 "receipt_ref": "sha256:" + "f" * 64},
            ]}]},
        {"node_id": "b", "hypothesis": "No source", "investigation": [
            {"channel": "external_tools", "status": "unavailable"}]},
        {"node_id": "c", "hypothesis": "No budget", "investigation": [
            {"channel": "external_tools", "status": "budget_limited"}]},
    ]
    scores = rank_branches(branches, critiques, beam_width=2)
    assert scores[0]["node_id"] == "a"
    assert scores[0]["evidence_count"] == 1
    assert sum(item["selected"] for item in scores) == 2
    assert scores[-1]["prune_reason"]
    assert scores[-1]["evidence_count"] == 0
    assert branches[0].get("score") is None  # inputs unchanged
