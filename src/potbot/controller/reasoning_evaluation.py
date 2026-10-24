"""R5: validate independent critique and rank evidence-backed reasoning branches.

Model scores are advisory; only host-receipted findings count as external
corroboration. No public or private chain-of-thought is required or retained.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from potbot.domain.errors import InvalidInputError

Recommendation = Literal["keep", "prune", "reject"]


@dataclass(frozen=True, slots=True)
class BranchCritique:
    node_id: str
    relevance: int
    evidence_alignment: int
    falsification_risk: int
    uncertainty: int
    recommendation: Recommendation
    rationale: str
    issues: tuple[str, ...]
    tests: tuple[str, ...]


def _load_json(raw: str) -> Any:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 32768:
        raise InvalidInputError("reasoning critique must be bounded")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError):
        raise InvalidInputError("invalid reasoning critique JSON") from None


def parse_critiques(raw: str, expected: set[str]) -> dict[str, BranchCritique]:
    """Strictly bind each evaluation to one known investigator node."""
    payload = _load_json(raw)
    if not isinstance(payload, dict) or set(payload) != {"evaluations"}:
        raise InvalidInputError("critique must contain evaluations only")
    items = payload["evaluations"]
    if not isinstance(items, list) or len(items) != len(expected) or len(items) > 12:
        raise InvalidInputError("critique must address every branch exactly once")
    result: dict[str, BranchCritique] = {}
    keys = {"node_id", "relevance", "evidence_alignment", "falsification_risk",
            "uncertainty", "recommendation", "rationale", "issues", "tests"}
    for item in items:
        if not isinstance(item, dict) or set(item) != keys:
            raise InvalidInputError("unsupported branch critique fields")
        node_id = item["node_id"]
        if node_id not in expected or node_id in result:
            raise InvalidInputError("unknown or duplicate critique branch")
        if any(type(item[name]) is not int or not 0 <= item[name] <= 4 for name in (
                "relevance", "evidence_alignment", "falsification_risk", "uncertainty")):
            raise InvalidInputError("critique ratings must be integers from 0 to 4")
        if item["recommendation"] not in {"keep", "prune", "reject"}:
            raise InvalidInputError("unsupported critique recommendation")
        if not isinstance(item["rationale"], str) or not 1 <= len(item["rationale"]) <= 1000:
            raise InvalidInputError("critique requires bounded rationale")
        for field in ("issues", "tests"):
            if not isinstance(item[field], list) or len(item[field]) > 6 or any(
                not isinstance(value, str) or not 1 <= len(value) <= 300 for value in item[field]
            ):
                raise InvalidInputError("critique lists must be bounded")
        result[node_id] = BranchCritique(
            node_id=node_id, relevance=item["relevance"],
            evidence_alignment=item["evidence_alignment"],
            falsification_risk=item["falsification_risk"], uncertainty=item["uncertainty"],
            recommendation=item["recommendation"], rationale=item["rationale"],
            issues=tuple(item["issues"]), tests=tuple(item["tests"]),
        )
    return result


def rank_branches(
    branches: list[dict[str, Any]], critiques: dict[str, BranchCritique], *, beam_width: int = 2,
) -> list[dict[str, Any]]:
    """Deterministic beam admission; model assertions never become evidence.

    Unverified, unavailable and budget-limited tool searches receive no evidence
    credit. Rejections without independently receipted contradictions may be
    pruned but never treated as objectively disproven.
    """
    if type(beam_width) is not int or not 1 <= beam_width <= 12:
        raise InvalidInputError("beam width must be 1-12")
    results: list[dict[str, Any]] = []
    for branch in branches:
        node_id = branch["node_id"]
        critique = critiques[node_id]
        findings = {
            (entry["source_ref"], entry["source_sha256"])
            for record in branch["investigation"]
            if record.get("status") == "completed"
            for entry in record.get("findings", [])
            if isinstance(entry, dict) and entry.get("receipt_ref")
        }
        coverage = len({r["channel"] for r in branch["investigation"] if r.get("status") == "completed"})
        # Ratings are bounded advisory input. Independent source receipts are
        # required for the extra evidence contribution and are never fabricated.
        score = max(0, min(100, 10 + 10 * critique.relevance
                           + 4 * critique.evidence_alignment
                           - 7 * critique.falsification_risk
                           - 4 * critique.uncertainty
                           + 6 * min(len(findings), 3) + 2 * min(coverage, 3)))
        results.append({**branch, "score": score, "evidence_count": len(findings),
                        "critique": {"recommendation": critique.recommendation,
                                     "rationale": critique.rationale,
                                     "issues": list(critique.issues),
                                     "tests": list(critique.tests)},
                        "evaluation_status": "advisory_scored"})
    ranked = sorted(results, key=lambda entry: (-entry["score"], entry["node_id"]))
    for index, branch in enumerate(ranked):
        branch["selected"] = index < beam_width
        branch["prune_reason"] = ("outside bounded evidence-weighted beam" if not branch["selected"]
                                  else None)
    return ranked
