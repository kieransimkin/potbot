#!/usr/bin/env python3
"""Validate the proposed memory plan, not implementation or runtime acceptance."""

from __future__ import annotations

import argparse
import copy
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "docs/object-memory"
KINDS = {"type", "thing", "event", "ember", "cog", "credential", "enigma", "pondle", "snug", "om"}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate(data: dict[str, Any], document: str) -> dict[str, int]:
    require(data["schema"] == "potbot.object-memory-plan.v1", "Plan schema")
    require(data["document_version"] == "1.0", "Document version")
    require(data["implementation_status"] == "not_started", "Plan is not runtime evidence")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", data["source_baseline"])), "Source baseline")
    require(
        len(data["native_kinds"]) == 10 and set(data["native_kinds"]) == KINDS,
        "Every native Syn kind needs a graph",
    )
    require(
        data["graph_identity"] == ["workspace_id", "namespace_id", "canonical_owner_key"],
        "Stable graph owner identity",
    )
    require(
        data["callable_acceptance_prerequisites"] == [f"C{i:02}" for i in range(1, 8)],
        "Mandatory C01-C07 Cog requirements",
    )
    requirements = {r["id"] for r in data["requirements"]}
    acceptance = {a["id"] for a in data["acceptance"]}
    expected_requirements = {f"R{i:02}" for i in range(1, 33)}
    expected_acceptance = {f"A{i:02}" for i in range(1, 33)}
    require(
        len(data["requirements"]) == 32 and requirements == expected_requirements, "Requirements"
    )
    require(len(data["acceptance"]) == 32 and acceptance == expected_acceptance, "Acceptance cases")
    for item in data["acceptance"]:
        require(
            item["runtime_status"] == "not_run", "A proposed case is not a passing runtime test"
        )
        require(bool(item["scenario"].strip()), "Acceptance scenario")
        require(f"| {item['id']} |" in document, "Missing documented acceptance case")
    stages = {s["id"]: s for s in data["stages"]}
    expected_stages = {f"MG{m:02}.{s}" for m in range(1, 9) for s in range(1, 5)}
    require(len(data["stages"]) == 32 and set(stages) == expected_stages, "Stage identities")
    covered_requirements: set[str] = set()
    covered_acceptance: set[str] = set()
    for identity, stage in stages.items():
        require(
            stage["status"] == "not_started" and stage["runtime_evidence"] == [], "Stage status"
        )
        require(stage["effort"] in {"S", "M", "L"}, "Relative effort")
        require(bool(stage["title"].strip()) and identity in document, "Documented stage")
        deps = stage["depends_on"]
        require(len(deps) == len(set(deps)), "Duplicate dependency")
        require(set(deps) <= set(stages) and identity not in deps, "Unknown or self dependency")
        refs, cases = set(stage["requirements"]), set(stage["acceptance"])
        require(bool(refs) and refs <= requirements, "Stage requirement reference")
        require(bool(cases) and cases <= acceptance, "Stage acceptance reference")
        require(
            bool(stage["original_steps"])
            and all(
                re.fullmatch(r"P\d{3}", step) and 1 <= int(step[1:]) <= 153
                for step in stage["original_steps"]
            ),
            "Original P001-P153 traceability",
        )
        covered_requirements.update(refs)
        covered_acceptance.update(cases)
    require(covered_requirements == requirements, "Uncovered requirement")
    require(covered_acceptance == acceptance, "Uncovered acceptance case")
    done: set[str] = set()
    visiting: set[str] = set()

    def visit(identity: str) -> None:
        require(identity not in visiting, "Dependency cycle")
        if identity in done:
            return
        visiting.add(identity)
        for dependency in stages[identity]["depends_on"]:
            visit(dependency)
        visiting.remove(identity)
        done.add(identity)

    for identity in stages:
        visit(identity)
    return {
        "stages": len(stages),
        "requirements": len(requirements),
        "acceptance_cases": len(acceptance),
    }


def negative_probes(data: dict[str, Any], document: str) -> int:
    samples: list[dict[str, Any]] = []
    changed = copy.deepcopy(data)
    changed["native_kinds"].remove("type")
    samples.append(changed)
    changed = copy.deepcopy(data)
    changed["stages"][0]["status"] = "complete"
    samples.append(changed)
    changed = copy.deepcopy(data)
    changed["stages"][0]["depends_on"] = ["MG99.1"]
    samples.append(changed)
    changed = copy.deepcopy(data)
    changed["stages"][0]["depends_on"] = ["MG01.2"]
    samples.append(changed)
    changed = copy.deepcopy(data)
    changed["acceptance"][0]["runtime_status"] = "passed"
    samples.append(changed)
    changed = copy.deepcopy(data)
    changed["requirements"].pop()
    samples.append(changed)
    changed = copy.deepcopy(data)
    changed["stages"][0]["acceptance"] = ["A99"]
    samples.append(changed)
    changed = copy.deepcopy(data)
    changed["graph_identity"].remove("namespace_id")
    samples.append(changed)
    for sample in samples:
        try:
            validate(sample, document)
        except ValueError:
            continue
        raise ValueError("A corrupted planning record passed a negative probe")
    return len(samples)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write a new planning receipt; never overwrite")
    args = parser.parse_args()
    data = json.loads((PLAN / "plan.json").read_text(encoding="utf-8"))
    document_path = PLAN / "implementation-plan.md"
    document = document_path.read_text(encoding="utf-8", errors="strict")
    result = validate(data, document)
    result["negative_probes"] = negative_probes(data, document)
    require(data["source_baseline"] in document, "Document/source baseline parity")
    for target in data["source_paths"]:
        path = (ROOT / target).resolve()
        require(path.is_relative_to(ROOT) and path.is_file(), f"Missing source authority: {target}")
    for target in re.findall(r"\]\(([^)]+)\)", document):
        if re.match(r"https?://|#", target):
            continue
        path = (document_path.parent / target.split("#", 1)[0]).resolve()
        require(path.is_relative_to(ROOT) and path.is_file(), f"Broken local reference: {target}")
    image = ET.fromstring((PLAN / "media/object-memory-architecture.svg").read_bytes())
    ns = {"s": "http://www.w3.org/2000/svg"}
    require(image.get("role") == "img", "Illustration accessibility role")
    require(
        image.find("s:title", ns) is not None and image.find("s:desc", ns) is not None,
        "Illustration accessible description",
    )
    receipt = {"valid": True, **result, "scope": "Planning consistency; no runtime acceptance"}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(receipt, stream, indent=2)
            stream.write("\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
