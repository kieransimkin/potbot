#!/usr/bin/env python3
"""Opt-in KeywordMoves source-checkout qualification runner (not a production sandbox).

Never use this local workflow adapter for untrusted source or model-generated
operations. Production Potbot dispatch must go through its qualified tool host.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from potbot.controller.keywordmoves_workflows import (
    KeywordMovesWorkflow,
    WorkflowPlan,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keywordmoves-checkout", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--trust-local-source", action="store_true", help=(
        "Explicitly permit importing and executing source from the selected checkout "
        "in this current host process (not sandboxed)."))
    args = parser.parse_args()
    if not args.trust_local_source:
        parser.error("source execution requires --trust-local-source")
    plan = WorkflowPlan.from_dict(json.loads(args.plan.read_text(encoding="utf-8")))
    checkout = args.keywordmoves_checkout.resolve(strict=True)
    revision = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    if revision != plan.source_revision:
        parser.error("KeywordMoves checkout is not at the exact pinned plan revision")
    sys.path.insert(0, str(checkout / "src"))
    from keywordmoves.models import ExecutionContext, PluginRequest
    from keywordmoves.registry import LLMRegistry, PluginRegistry

    registry, llms = PluginRegistry(), LLMRegistry()
    supported = {item.name: set(item.operations) for item in registry.describe()}

    def invoke(*, plugin, operation, inputs, keywords, options):
        if plugin not in supported or operation not in supported[plugin]:
            raise ValueError("KeywordMoves plugin/operation not present in pinned checkout")
        result = registry.get(plugin).run(
            PluginRequest(operation, keywords=keywords, inputs=inputs, options=options),
            ExecutionContext(llms=llms),
        )
        return result.to_dict()

    flow = KeywordMovesWorkflow(plan=plan, input_root=args.inputs,
                                output_root=args.output, invoke=invoke,
                                emit=lambda name, data: print(json.dumps({
                                    "event": name, "data": data}, sort_keys=True), flush=True))
    result = flow.run(resume=args.resume)
    print(json.dumps({"status": result.status, "steps": result.completed,
                      "waiting_for": result.waiting_for, "artifacts": result.artifacts,
                      "plan_sha256": result.plan_sha256}, indent=2))
    return 0 if result.status == "completed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
