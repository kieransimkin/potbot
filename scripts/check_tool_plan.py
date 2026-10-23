#!/usr/bin/env python3
"""Check roadmap parity and dependency/requirement integrity, not runtime acceptance."""

from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "docs/potbot-tool-mcp-implementation-bundle"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def dependencies(value: str, ids: set[str]) -> list[str]:
    result: set[str] = set()
    for token in value.split(","):
        token = token.strip()
        if token == "none":
            continue
        if re.fullmatch(r"T\d{2}\.\*", token):
            matches = {i for i in ids if i.startswith(token[:-1])}
            require(bool(matches), f"Unresolved dependency: {token}")
            result.update(matches)
        elif re.fullmatch(r"T\d{2}\.\d–T\d{2}\.\d", token):
            start, end = token.split("–")
            require(start[:4] == end[:4], f"Cross-phase stage range: {token}")
            require(int(start[4:]) <= int(end[4:]), f"Reversed range: {token}")
            result.update(f"{start[:4]}{n}" for n in range(int(start[4:]), int(end[4:]) + 1))
        else:
            require(bool(re.fullmatch(r"T\d{2}\.\d", token)), f"Unparsed dependency: {token}")
            result.add(token)
    require(result <= ids, f"Unknown dependency: {result - ids}")
    return sorted(result)


def validate(
    plan: str, stages: list[dict[str, Any]], rows: list[dict[str, str]], registry: dict[str, Any]
) -> dict[str, int]:
    ids = {s["id"] for s in stages}
    expected = {f"T{phase:02}.{stage}" for phase in range(1, 13) for stage in range(1, 5)}
    require(len(stages) == len(ids) == len(rows) == 48 and ids == expected, "Stage identities")
    require(len({row["Stage"] for row in rows}) == 48, "Duplicate CSV stage")
    require("**Document version:** 1.4" in plan, "Plan version")
    require(registry["document_version"] == "1.4", "Register version")
    require(registry["status"] == "requirements_only", "False runtime status")
    cog_contract = registry["cog_contract"]
    require(cog_contract["mandatory"] is True, "Cog integration is mandatory")
    require(cog_contract["agent_facing_interface"] == "registered_syn_cog", "Cog interface")
    require(cog_contract["direct_transport_dispatch"] is False, "Raw transport bypass")
    require(cog_contract["single_registered_input_type"] is True, "Single Cog input")
    require(cog_contract["single_registered_output_type"] is True, "Single Cog output")
    require(cog_contract["execution_authority"] == "controller_minted_out_of_band", "Cog authority")
    cog_ids = {r["id"] for r in cog_contract["requirements"]}
    require(cog_ids == {f"C{n:02}" for n in range(1, 7)}, "Cog contract coverage")
    gap_ids = {g["id"] for g in registry["gaps"]}
    require(len(gap_ids) == len(registry["gaps"]) == 18, "Gap identities")
    fixtures = {g["workflow"] for g in registry["gaps"]}
    require(fixtures == {f"WF{n:02}" for n in range(16, 34)}, "Gap fixture coverage")
    workflows = re.findall(r"^\*\*(WF\d{2}) — ", plan, re.M)
    require(
        len(workflows) == 33 and set(workflows) == {f"WF{n:02}" for n in range(1, 34)},
        "Missing or duplicated workflow scenario",
    )
    csv_by_id = {row["Stage"]: row for row in rows}
    graph = {}
    for stage in stages:
        identity = stage["id"]
        require(stage["status"] == "not_started", f"Planning changed status: {identity}")
        require(stage["phase"] == int(identity[1:3]), f"Stage phase: {identity}")
        require(stage["stage"] == int(identity[4:]), f"Stage number: {identity}")
        edges = dependencies(stage["prerequisites"], ids)
        require(edges == stage["hard_prerequisites"], f"Dependency expansion: {identity}")
        require(identity not in edges, f"Self dependency: {identity}")
        graph[identity] = edges
        pattern = rf"#### {re.escape(identity)} — ([^\n]+)\n(.*?)(?=\n#### |\n\*\*Phase |\Z)"
        blocks = list(re.finditer(pattern, plan, re.S))
        require(len(blocks) == 1, f"Missing/duplicate Markdown stage: {identity}")
        block = blocks[0]
        require(block.group(1) == stage["name"], f"Markdown name: {identity}")
        row = csv_by_id[identity]
        require(row["Stage name"] == stage["name"], f"CSV name: {identity}")
        require(row["Phase"] == f"Phase {stage['phase']:02}", f"CSV phase: {identity}")
        require(row["Status"] == "Not started", f"CSV status: {identity}")
        for field, label, column in (
            ("prerequisites", "Prerequisites", "Prerequisites"),
            ("deliverable", "Deliverable", "Deliverable"),
            ("implementation", "Implementation", None),
            ("acceptance", "Automated acceptance", "Acceptance checks"),
            ("demonstration", "Demonstration", "Demonstration"),
        ):
            matches = re.findall(rf"^- \*\*{label}:\*\* ([^\n]+)", block.group(2), re.M)
            value = stage[field]
            if field in {"prerequisites", "deliverable"} and not value.endswith("."):
                value += "."
            require(matches == [value], f"Markdown {field}: {identity}")
            if column:
                require(row[column] == stage[field], f"CSV {field}: {identity}")
        owned = {g["id"] for g in registry["gaps"] if identity in g["stages"]}
        require(set(stage["gap_requirements"]) == owned, f"Requirement mapping: {identity}")
        require(owned <= gap_ids, f"Unknown gap: {identity}")
        require(set(stage["cog_requirements"]) == cog_ids, f"Cog requirement mapping: {identity}")

    visited: set[str] = set()
    active: set[str] = set()

    def visit(identity: str) -> None:
        require(identity not in active, f"Dependency cycle at {identity}")
        if identity in visited:
            return
        active.add(identity)
        for edge in graph[identity]:
            visit(edge)
        active.remove(identity)
        visited.add(identity)

    for identity in sorted(ids):
        visit(identity)
    for gap in registry["gaps"]:
        require(bool(gap["stages"]) and set(gap["stages"]) <= ids, f"Unowned gap: {gap['id']}")
        require(gap["status"] == "planned", f"False completion: {gap['id']}")
        require(gap["priority"] in {"P0", "P1"}, f"Gap priority: {gap['id']}")
        for field in ("baseline_coverage", "implementation", "acceptance", "demonstration"):
            require(bool(gap[field].strip()), f"Empty {field}: {gap['id']}")
        require(f"**{gap['workflow']} — {gap['title']}:**" in plan, f"Workflow: {gap['id']}")
    for change in registry["dependency_changes"]:
        stage = next(s for s in stages if s["id"] == change["stage"])
        require(stage["prerequisites"] == change["current"], "Stale dependency change receipt")
        require(change["previous"] != change["current"], "Empty dependency change")
    return {
        "stages": 48,
        "gaps": 18,
        "workflow_scenarios": 33,
        "dependency_edges": sum(map(len, graph.values())),
    }


def check_documents(registry: dict[str, Any]) -> None:
    spec = ROOT / "docs/implementation/09-tool-plan-gap-audit.md"
    text = spec.read_text(encoding="utf-8", errors="strict")
    for gap in registry["gaps"]:
        require(f"### {gap['id']} — {gap['title']}" in text, f"Specification gap: {gap['id']}")
        for field in ("baseline_coverage", "implementation", "acceptance", "demonstration"):
            require(gap[field] in text, f"Specification drift: {gap['id']} {field}")
    for source in (
        spec,
        ROOT / "README.md",
        BUNDLE / "potbot-universal-tool-mcp-implementation-plan.md",
    ):
        for target in re.findall(r"\]\(([^)]+)\)", source.read_text(encoding="utf-8")):
            if re.match(r"https?://|#|mailto:", target):
                continue
            path = (source.parent / target.split("#", 1)[0]).resolve()
            require(
                path.is_relative_to(ROOT) and path.exists(),
                f"Broken local reference: {source.name}: {target}",
            )
    image = ROOT / "docs/implementation/media/tool-plan-qualification.svg"
    tree = ET.fromstring(image.read_bytes())
    require(tree.get("role") == "img", "SVG accessibility role")
    ns = {"s": "http://www.w3.org/2000/svg"}
    require(
        tree.find("s:title", ns) is not None and tree.find("s:desc", ns) is not None,
        "SVG description",
    )
    cog_spec = ROOT / "docs/implementation/10-syn-tool-cogs.md"
    cog_text = cog_spec.read_text(encoding="utf-8", errors="strict")
    for term in (
        "CogSignature",
        "TypedCogValue",
        "CogRunContext",
        "CogReceipt",
        "Python-first Cog acceptance",
        "out of band",
        "one-input/one-output",
        "zero tool dispatch",
    ):
        require(term in cog_text, f"Missing Cog contract: {term}")
    for target in re.findall(r"\]\(([^)]+)\)", cog_text):
        if not re.match(r"https?://|#", target):
            path = (cog_spec.parent / target).resolve()
            require(path.is_relative_to(ROOT) and path.exists(), f"Broken Cog reference: {target}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write a fresh JSON receipt; never overwrites")
    args = parser.parse_args()
    plan = (BUNDLE / "potbot-universal-tool-mcp-implementation-plan.md").read_text(
        encoding="utf-8", errors="strict"
    )
    stages = json.loads(
        (BUNDLE / "potbot-tool-integration-stages.json").read_text(encoding="utf-8")
    )
    rows = list(
        csv.DictReader(
            io.StringIO(
                (BUNDLE / "potbot-tool-integration-48-stage-tracker.csv").read_text(
                    encoding="utf-8"
                )
            )
        )
    )
    registry = json.loads(
        (BUNDLE / "potbot-tool-plan-gap-register.json").read_text(encoding="utf-8")
    )
    counts = validate(plan, stages, rows, registry)
    check_documents(registry)
    rejected = []
    # In-memory corruptions prove this gate rejects plan defects; no runtime claims.
    for name in ("cycle", "missing-stage", "tracker-drift", "unowned-requirement", "cog-bypass"):
        p, s, r, g = plan, copy.deepcopy(stages), copy.deepcopy(rows), copy.deepcopy(registry)
        if name == "cycle":
            s[0]["prerequisites"] = "T01.2"
            s[0]["hard_prerequisites"] = ["T01.2"]
            r[0]["Prerequisites"] = "T01.2"
            p = p.replace("- **Prerequisites:** none.", "- **Prerequisites:** T01.2.", 1)
        elif name == "missing-stage":
            s.pop()
        elif name == "tracker-drift":
            r[0]["Acceptance checks"] = "unverified"
        elif name == "unowned-requirement":
            g["gaps"][0]["stages"] = []
        else:
            g["cog_contract"]["direct_transport_dispatch"] = True
        try:
            validate(p, s, r, g)
        except ValueError as error:
            rejected.append({"case": name, "rejection": str(error)})
        else:
            raise ValueError(f"Negative probe unexpectedly passed: {name}")
    receipt = {
        "status": "passed",
        "scope": "plan_consistency_only",
        **counts,
        "negative_probes": rejected,
    }
    output = json.dumps(receipt, indent=2) + "\n"
    if args.output:
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(output)
    print(output, end="")


if __name__ == "__main__":
    main()
