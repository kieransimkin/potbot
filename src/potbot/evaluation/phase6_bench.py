"""Offline, deterministic Phase 6 programming benchmark.

All source snippets in this module are authored test fixtures, not downloaded
code or model responses. Actual candidate patches are applied to isolated
checkout copies, compiled, and checked with unittest. The runner does not
qualify any real model or sandbox any untrusted third-party source.
"""
from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from potbot.cogs.builtin import COMPLETION_TEXT
from potbot.cogs.contracts import CodingScope, CogRunContext, TypedCogValue
from potbot.cogs.registry import (
    COG_COMPLETION_JSON,
    COG_JSON_CODING,
    COG_SCOPE_CODING,
    BoundCogRegistry,
)
from potbot.controller.search_scheduler import DepthFirstSearch, FrontierItem, SearchStatus
from potbot.domain.model_proposals import ModelCodingResponse
from potbot.domain.search_graph import (
    EdgeKind,
    GraphEdge,
    GraphNode,
    GraphProjection,
    KnowledgeView,
    NodeKind,
    SolvePolicy,
)
from potbot.domain.serialization import canonical_json, content_hash


@dataclass(frozen=True, slots=True)
class BenchCase:
    case_id: str
    summary: str
    baseline: dict[str, str]
    filename: str
    candidates: tuple[tuple[str, str], ...]
    expect_fix: bool = True


CASES: tuple[BenchCase, ...] = (
    BenchCase("arithmetic", "Fix an addition regression", {
        "target.py": "def add(a, b):\n    return a - b\n",
        "test_target.py": "import unittest\nfrom target import add\nclass T(unittest.TestCase):\n def test_sum(self): self.assertEqual(add(3, 4), 7)\n def test_negative(self): self.assertEqual(add(-2, 5), 3)\n",
    }, "target.py", (
        ("fixture-coder-a", "def add(a, b):\n    return a * b\n"),
        ("fixture-coder-b", "def add(a, b):\n    return a + b\n"),
    )),
    BenchCase("boundary", "Correct inclusive upper bound", {
        "target.py": "def upto(n):\n    return list(range(n))\n",
        "test_target.py": "import unittest\nfrom target import upto\nclass T(unittest.TestCase):\n def test_three(self): self.assertEqual(upto(3), [0,1,2,3])\n def test_zero(self): self.assertEqual(upto(0), [0])\n",
    }, "target.py", (
        ("fixture-coder-a", "def upto(n):\n    return list(range(1, n+1))\n"),
        ("fixture-coder-b", "def upto(n):\n    return list(range(n+1))\n"),
    )),
    BenchCase("normalization", "Handle whitespace and case", {
        "target.py": "def normalize(name):\n    return name.lower()\n",
        "test_target.py": "import unittest\nfrom target import normalize\nclass T(unittest.TestCase):\n def test_spaces(self): self.assertEqual(normalize('  AbC  '), 'abc')\n def test_internal(self): self.assertEqual(normalize(' A  B '), 'a  b')\n",
    }, "target.py", (
        ("fixture-coder-a", "def normalize(name):\n    return name.replace(' ', '').lower()\n"),
        ("fixture-coder-b", "def normalize(name):\n    return name.strip().lower()\n"),
    )),
    BenchCase("reuse", "Reuse an existing helper", {
        "helpers.py": "def clamp(value, lower, upper):\n    return min(upper, max(lower, value))\n",
        "target.py": "def percent(value):\n    return value\n",
        "test_target.py": "import unittest\nfrom target import percent\nclass T(unittest.TestCase):\n def test_positive(self): self.assertEqual(percent(140), 100)\n def test_negative(self): self.assertEqual(percent(-5), 0)\n def test_center(self): self.assertEqual(percent(45), 45)\n",
    }, "target.py", (
        ("fixture-coder-a", "def percent(value):\n    return max(0, value)\n"),
        ("fixture-coder-b", "from helpers import clamp\n\ndef percent(value):\n    return clamp(value, 0, 100)\n"),
    )),
    BenchCase("regression", "Do not fix one behavior by breaking another", {
        "target.py": "def parity(n):\n    return 'odd'\n",
        "test_target.py": "import unittest\nfrom target import parity\nclass T(unittest.TestCase):\n def test_even(self): self.assertEqual(parity(4), 'even')\n def test_odd(self): self.assertEqual(parity(3), 'odd')\n",
    }, "target.py", (
        ("fixture-coder-a", "def parity(n):\n    return 'even'\n"),
        ("fixture-coder-b", "def parity(n):\n    return 'even' if n % 2 == 0 else 'odd'\n"),
    )),
    BenchCase("unresolved", "No fabricated success when candidates remain incorrect", {
        "target.py": "def exponent(a, b):\n    return a + b\n",
        "test_target.py": "import unittest\nfrom target import exponent\nclass T(unittest.TestCase):\n def test_power(self): self.assertEqual(exponent(3, 3), 27)\n def test_zero(self): self.assertEqual(exponent(5, 0), 1)\n",
    }, "target.py", (
        ("fixture-coder-a", "def exponent(a, b):\n    return a * b\n"),
        ("fixture-coder-b", "def exponent(a, b):\n    return a ** (b+1)\n"),
    ), expect_fix=False),
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _run(root: Path, timeout: int = 5) -> dict[str, Any]:
    # Fixture-only subprocesses. This is not an untrusted-code sandbox and the
    # caller must never accept model-provided files through this benchmark API.
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMPDIR"}}
    env["PYTHONPATH"] = str(root)
    t0 = time.perf_counter()
    try:
        p = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-q"],
            cwd=root, env=env, capture_output=True, text=True, timeout=timeout, check=False,
        )
        return {"passed": p.returncode == 0, "exit_code": p.returncode,
                "duration_ms": round((time.perf_counter() - t0) * 1000),
                "diagnostic_digest": _sha((p.stdout + p.stderr)[:16384])}
    except subprocess.TimeoutExpired:
        return {"passed": False, "exit_code": None,
                "duration_ms": round((time.perf_counter() - t0) * 1000),
                "failure": "deadline", "diagnostic_digest": None}


def _write_files(directory: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        # Static fixture names only; callers cannot supply an arbitrary repo.
        if name not in {"target.py", "test_target.py", "helpers.py"}:
            raise ValueError("Only registered fixture paths may be materialized")
        (directory / name).write_text(content, encoding="utf-8")


def _patch(filename: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile="a/" + filename, tofile="b/" + filename,
    ))


def _apply_and_verify(base: Path, case: BenchCase, contents: str, root: Path) -> dict[str, Any]:
    patch = _patch(case.filename, case.baseline[case.filename], contents)
    if not patch:
        return {"passed": False, "reason": "no_change", "patch_digest": _sha(patch)}
    shutil.copytree(base, root)
    start = time.perf_counter()
    try:
        checked = subprocess.run(["git", "apply", "--check", "-"], input=patch,
                                 cwd=root, capture_output=True, text=True, timeout=5)
        if checked.returncode != 0:
            return {"passed": False, "reason": "unapplicable", "patch_digest": _sha(patch)}
        applied = subprocess.run(["git", "apply", "-"], input=patch,
                                 cwd=root, capture_output=True, text=True, timeout=5)
        if applied.returncode != 0:
            return {"passed": False, "reason": "apply_failed", "patch_digest": _sha(patch)}
        # Every attempt starts from the same base and may not alter test files.
        for name, original in case.baseline.items():
            if name != case.filename and (root / name).read_text(encoding="utf-8") != original:
                return {"passed": False, "reason": "test_modified", "patch_digest": _sha(patch)}
        checked_result = _run(root)
        return {"patch_digest": _sha(patch), "elapsed_ms": round((time.perf_counter()-start)*1000),
                "changed_files": [case.filename], "reuse": "from helpers import clamp" in contents,
                **checked_result}
    except subprocess.TimeoutExpired:
        return {"passed": False, "reason": "deadline", "patch_digest": _sha(patch)}


def benchmark(*, max_candidates: int = 2) -> dict[str, Any]:
    """Measure observed search behavior on built-in deterministic fixture cases."""
    if type(max_candidates) is not int or not 1 <= max_candidates <= 2:
        raise ValueError("The deterministic benchmark supports one or two attempts")
    rows: list[dict[str, Any]] = []
    for case in CASES:
        with tempfile.TemporaryDirectory(prefix="potbot-phase6-bench-") as temporary:
            scratch = Path(temporary)
            base = scratch / "baseline"
            base.mkdir()
            _write_files(base, case.baseline)
            baseline = _run(base)
            attempts = []
            workspace, task, contract, base_state = (uuid4() for _ in range(4))
            policy = SolvePolicy(
                workspace, task, contract, base_state, "a" * 64,
                ("fixture-reasoner",), ("fixture-coder-a", "fixture-coder-b"),
            )
            view = KnowledgeView(workspace, task, contract, base_state,
                                 policy.semantic_release_digest, ())
            graph = GraphProjection(policy)
            root_node = GraphNode(uuid4(), NodeKind.ROOT, view, "controller",
                                  policy.digest, 0)
            graph.add_node(root_node)
            registry = BoundCogRegistry.builtins()
            original_hashes = {name: _sha(value) for name, value in case.baseline.items()}
            scheduler = DepthFirstSearch(policy)
            scheduler.anchor(FrontierItem(root_node.id, None, 0, policy.digest, "controller"))
            lookup: dict[object, tuple[int, str, str]] = {}
            # Reverse insertion for a depth-first LIFO; first listed model runs first.
            for ordinal in reversed(range(min(max_candidates, len(case.candidates)))):
                producer, content = case.candidates[ordinal]
                candidate_id = uuid4()
                scheduler.offer(FrontierItem(
                    candidate_id, root_node.id, 1, content_hash({"patch": content}), producer,
                ))
                lookup[candidate_id] = (ordinal, producer, content)
            while (claim := scheduler.next()) is not None:
                ordinal, producer, content = lookup[claim.node_id]
                patch = _patch(case.filename, case.baseline[case.filename], content)
                response = ModelCodingResponse(
                    implementation_id=uuid4(), base_state_id=base_state,
                    contract_revision_id=contract, patch=patch.encode("utf-8"),
                    changed_files=(case.filename,),
                )
                cog_context = CogRunContext(
                    cog_snapshot_hash=registry.snapshot.fingerprint,
                    task_revision_id=contract, repository_state_id=base_state,
                    invocation_id=uuid4(),
                )
                scope = CodingScope(
                    response.implementation_id, base_state, contract,
                    permitted_paths=(case.filename,), known_checks=("unittest",),
                )
                cogs = registry.chain(
                    (COG_COMPLETION_JSON, COG_JSON_CODING, COG_SCOPE_CODING),
                    TypedCogValue(COMPLETION_TEXT, canonical_json(response.to_dict())),
                    cog_context, scope=scope,
                )
                converted = ModelCodingResponse.from_dict(cogs[-1].value.value)
                if converted.patch != patch.encode("utf-8"):
                    raise RuntimeError("Cog conversion changed the proposed patch")
                proposal_node = GraphNode(uuid4(), NodeKind.PROPOSAL, view, producer,
                                          content_hash({"proposal": producer, "patch": patch}), 1)
                graph.add_node(proposal_node)
                graph.add_edge(GraphEdge(root_node.id, proposal_node.id, EdgeKind.DERIVED_FROM))
                candidate_node = GraphNode(uuid4(), NodeKind.CANDIDATE, view, producer,
                                           content_hash({"candidate": patch}), 2)
                graph.add_node(candidate_node)
                graph.add_edge(GraphEdge(proposal_node.id, candidate_node.id, EdgeKind.CONTINUES))
                result = _apply_and_verify(base, case, content, scratch / f"candidate-{ordinal}")
                observation = GraphNode(uuid4(), NodeKind.VERIFICATION, view,
                                        "unittest-verifier", content_hash({"result": result}), 3)
                graph.add_node(observation)
                graph.add_edge(GraphEdge(candidate_node.id, observation.id, EdgeKind.VERIFIES))
                scheduler.settle(
                    claim, SearchStatus.VERIFIED if result.get("passed") else SearchStatus.FAILED,
                    content_hash({"candidate": ordinal, "result": result}),
                )
                attempts.append({"producer": producer, "rank": ordinal + 1,
                                 "cog_receipt_chain": [step.receipt.fingerprint for step in cogs],
                                 "graph_candidate_id": str(candidate_node.id), **result})
            if original_hashes != {name: _sha((base/name).read_text(encoding="utf-8"))
                                   for name in case.baseline}:
                raise RuntimeError("Benchmark modified its immutable baseline")
            winners = [a for a in attempts if a["passed"]]
            selected = min(winners, key=lambda item: (not item.get("reuse", False), item["rank"])) if winners else None
            rows.append({"case_id": case.case_id, "baseline_failed": not baseline["passed"],
                         "candidate_count": len(attempts), "attempts": attempts,
                         "selected": selected["producer"] if selected else None,
                         "verified": selected is not None, "expected_fix": case.expect_fix,
                         "correct_stop": (selected is not None) == case.expect_fix,
                         "graph_nodes": len(graph.nodes), "graph_edges": len(graph.edges),
                         "cog_steps": sum(len(a["cog_receipt_chain"]) for a in attempts),
                         "search": scheduler.summary(),
                         "backtracks": sum(e.kind == "backtracked" for e in scheduler.events),
                         "scheduler_integrity": scheduler.integrity_digest()})
    n = len(rows)
    return {
        "schema_version": 1,
        "benchmark": "potbot-phase6-offline-programming-fixtures-v1",
        "classification": "synthetic-fixture; not a live model evaluation",
        "model_providers_tested": [],
        "isolation": "separate temporary candidate directories; no OS sandbox",
        "n_cases": n,
        "n_attempts": sum(row["candidate_count"] for row in rows),
        "total_graph_nodes": sum(row["graph_nodes"] for row in rows),
        "total_graph_edges": sum(row["graph_edges"] for row in rows),
        "total_cog_steps": sum(row["cog_steps"] for row in rows),
        "total_backtracks": sum(row["backtracks"] for row in rows),
        "search_policy": "deterministic-lifo-depth-first-v1",
        "baseline_failures": sum(row["baseline_failed"] for row in rows),
        "verified_successes": sum(row["verified"] for row in rows),
        "correct_stops": sum(row["correct_stop"] for row in rows),
        "observed_success_by_1": round(sum(row["attempts"][0]["passed"] for row in rows) / n, 6),
        "observed_success_by_k": round(sum(row["verified"] for row in rows) / n, 6),
        "case_results": rows,
    }


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Offline Potbot controller benchmark (fixtures only)")
    parser.add_argument("--max-candidates", type=int, choices=(1, 2), default=2)
    parser.add_argument("--output", default=None, help="Optional JSON result file")
    args = parser.parse_args()
    result = benchmark(max_candidates=args.max_candidates)
    result_json = canonical_json(result)
    if args.output is not None:
        from pathlib import Path
        Path(args.output).write_text(result_json + "\n", encoding="utf-8")
    print(result_json)
    return 0 if result["baseline_failures"] == result["n_cases"] and (args.max_candidates < 2 or result["correct_stops"] == result["n_cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
