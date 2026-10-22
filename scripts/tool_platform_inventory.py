#!/usr/bin/env python3
"""Offline, read-only Potbot tool-platform source inventory (T01.1).

Checks only known repository paths. Does not import plugins, invoke tooling,
query credentials, provision databases, or claim executable qualification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXPECTED_BASELINE = "fcede56c0390fa478acc79bf416468508fc0afc1"


@dataclass(frozen=True)
class SourceAnchor:
    component: str
    owner: str
    path: str
    decision: str
    purpose: str


ANCHORS: tuple[SourceAnchor, ...] = (
    SourceAnchor(
        "project_readme", "build", "README.md", "qualify", "Mandatory project build metadata target"
    ),
    SourceAnchor(
        "syn_parser",
        "semantics",
        "src/potbot/domain/syn.py",
        "reuse",
        "Canonical SYN Type and instance references",
    ),
    SourceAnchor(
        "syn_catalog",
        "semantics",
        "src/potbot/semantics/catalog.py",
        "reuse",
        "Bundled Type resolution",
    ),
    SourceAnchor(
        "hierarchy",
        "semantics",
        "src/potbot/semantics/hierarchy.py",
        "reuse",
        "Selected opt-in semantic hierarchy",
    ),
    SourceAnchor(
        "ember_contracts",
        "semantics",
        "src/potbot/semantics/embers.py",
        "reuse",
        "Inherited Ember contracts and binding semantics",
    ),
    SourceAnchor(
        "cog_contracts",
        "cogs",
        "src/potbot/cogs/contracts.py",
        "reuse",
        "Single-input single-output transform receipts",
    ),
    SourceAnchor(
        "cog_registry",
        "cogs",
        "src/potbot/cogs/registry.py",
        "extend",
        "Installed binding and approved Cog execution",
    ),
    SourceAnchor(
        "tool_definitions",
        "execution",
        "src/potbot/domain/execution.py",
        "reuse",
        "Controller-approved local tool definitions",
    ),
    SourceAnchor(
        "tool_gateway",
        "execution",
        "src/potbot/tools/gateway.py",
        "extend",
        "Execution admission, worker/lease recovery",
    ),
    SourceAnchor(
        "sandbox",
        "security",
        "src/potbot/workspace_sandbox.py",
        "reuse",
        "Workspace root and binding",
    ),
    SourceAnchor(
        "isolation",
        "security",
        "src/potbot/tools/isolation.py",
        "qualify",
        "Linux process containment and fail-closed policy",
    ),
    SourceAnchor(
        "identity",
        "security",
        "src/potbot/semantics/agent_security.py",
        "reuse",
        "Ed25519 public verification and locking Cog types",
    ),
    SourceAnchor(
        "credential_broker",
        "security",
        "src/potbot/security/broker.py",
        "extend",
        "Never expose private keys to model-visible results",
    ),
    SourceAnchor(
        "budgets",
        "orchestration",
        "src/potbot/controller/budgets.py",
        "reuse",
        "Controller-owned spending limits",
    ),
    SourceAnchor(
        "jobs",
        "orchestration",
        "src/potbot/controller/jobs.py",
        "extend",
        "Durable jobs and fenced workers",
    ),
    SourceAnchor(
        "solve_graph",
        "orchestration",
        "src/potbot/controller/solve_store.py",
        "extend",
        "Reasoning search graph and receipts",
    ),
    SourceAnchor(
        "cog_bridge",
        "orchestration",
        "src/potbot/controller/cog_bridge.py",
        "extend",
        "Typed data bridge between agent paths",
    ),
    SourceAnchor(
        "rest", "api", "src/potbot/api/server.py", "extend", "Versioned tool API projections"
    ),
    SourceAnchor(
        "research_transport",
        "research",
        "src/potbot/research/http_transport.py",
        "qualify",
        "Bounded, policy-gated outbound HTTP precursor",
    ),
    SourceAnchor(
        "research_cog",
        "research",
        "src/potbot/research/cog.py",
        "qualify",
        "Phase 7 research transformation, import pending",
    ),
    SourceAnchor(
        "benchmarks",
        "evaluation",
        "src/potbot/evaluation/benchmark_ledger.py",
        "extend",
        "Synthetic vs live distinctions",
    ),
    SourceAnchor(
        "migration_history",
        "persistence",
        "src/potbot/persistence/schema.py",
        "reuse",
        "Preserve previous migration ledgers",
    ),
    SourceAnchor(
        "stage_runner",
        "ci",
        "scripts/check.py",
        "extend",
        "uv/pytest/mypy/Ruff/Postgres/sandbox gates",
    ),
    SourceAnchor(
        "github_ci",
        "ci",
        ".github/workflows/ci.yml",
        "extend",
        "Linux real database and sandbox checks",
    ),
    SourceAnchor(
        "mcp_adapter",
        "adapters",
        "src/potbot/tools/mcp_client.py",
        "missing",
        "Official MCP SDK adapter (T02.1)",
    ),
    SourceAnchor(
        "browser_adapter",
        "adapters",
        "src/potbot/tools/browser_client.py",
        "missing",
        "Browser/Playwright adapter (T06)",
    ),
    SourceAnchor(
        "reasoning_solvers",
        "adapters",
        "src/potbot/tools/solver_client.py",
        "missing",
        "Solver and symbolic math adapter (T09)",
    ),
)


def _safe_path(root: Path, relative: str) -> Path:
    if relative.startswith("/") or "\\" in relative or ".." in Path(relative).parts:
        raise ValueError("Unexpected inventory anchor path")
    path = root
    for fragment in Path(relative).parts:
        path = path / fragment
        if path.is_symlink():
            raise ValueError(f"Source anchor is a symlink: {relative}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Source anchor escapes repository root")
    return path


def _git_revision(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            check=False,
            timeout=5,
            text=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and len(value) == 40 else None


def build_inventory(root: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Repository root must be a directory")
    anchors: list[dict[str, Any]] = []
    for anchor in ANCHORS:
        path = _safe_path(root, anchor.path)
        present = path.is_file()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if present else None
        anchors.append(
            {
                "component": anchor.component,
                "owner": anchor.owner,
                "path": anchor.path,
                "decision": anchor.decision,
                "purpose": anchor.purpose,
                "presence": "present" if present else "absent",
                "sha256": digest,
                "qualification": "not_established_by_inventory",
            }
        )
    counts = {
        d: sum(1 for a in anchors if a["decision"] == d)
        for d in ("reuse", "extend", "qualify", "missing")
    }
    missing_required = sorted(
        a["path"] for a in anchors if a["presence"] == "absent" and a["decision"] != "missing"
    )
    unexpected_new = sorted(
        a["path"] for a in anchors if a["presence"] == "present" and a["decision"] == "missing"
    )
    result: dict[str, Any] = {
        "schema_version": 1,
        "stage_id": "T01.1",
        "planning_baseline": EXPECTED_BASELINE,
        "checkout_head": _git_revision(root),
        "qualification": "read_only_source_presence_and_hashes_only",
        "components": anchors,
        "decision_counts": counts,
        "missing_required_anchors": missing_required,
        "unexpected_preexisting_components": unexpected_new,
        "next_steps": [
            "Confirm semantic Type identities against the selected release",
            "Qualify Linux sandbox, PostgreSQL migrations, scope, and RLS on real services",
            "Verify Phase 7 research Cog is installed under a pinned semantic release",
            "Only dispatch new protocol handlers after explicit controller authorization",
        ],
    }
    canonical = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    result["inventory_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--assert-required",
        action="store_true",
        help="Fail for missing expected pre-existing source",
    )
    parser.add_argument(
        "--expect-head", help="Optional exact Git HEAD; useful for an immutable baseline"
    )
    args = parser.parse_args()
    result = build_inventory(args.root)
    report = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report, end="")
    if args.expect_head and args.expect_head != result["checkout_head"]:
        print("Inventory HEAD does not match requested baseline", flush=True)
        return 3
    if args.assert_required and result["missing_required_anchors"]:
        print("Required source anchors are missing", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
