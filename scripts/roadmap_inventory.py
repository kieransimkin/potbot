#!/usr/bin/env python3
"""U00-A source inventory and namespaced stage crosswalk, never runtime qualification.

Read repository documentation without importing it, executing examples, following
external references, or fetching anything. Refresh is explicit; check is read-only.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = "docs/implementation/roadmap-inventory.json"
POLICY = "docs/implementation/roadmap-inventory-policy.json"
REVIEW = "docs/implementation/roadmap-review.json"
SCHEMA = "potbot.roadmap-inventory/1"
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_XML_BYTES = 8 * 1024 * 1024
DOC_SUFFIXES = {".md", ".rst", ".adoc", ".docx"}
MEDIA_SUFFIXES = {".png", ".svg", ".webm", ".mp4", ".jpg", ".jpeg"}
WORD_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
REQUIRED_PROGRAMMES = {
    "plan-1.0",
    "plan-1.1",
    "architecture-1.1",
    "original-user-requirements",
    "embedding-addendum",
    "semantic-addendum",
    "tools",
    "tool-gaps",
    "tool-cog-contract",
    "object-memory",
    "object-memory-requirements",
    "object-memory-acceptance",
    "modernization",
    "benchmarks",
    "unified",
    "recent-development",
    "worker-reconciliation",
    "syn-release",
    "compatibility",
    "external-skills",
    "worker-gates",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def safe_source(root: Path, name: str) -> Path:
    path = PurePosixPath(name)
    require(
        bool(name)
        and path.as_posix() == name
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in name
        and ":" not in name,
        f"Unsafe source path: {name!r}",
    )
    current = root
    for part in path.parts:
        current = current / part
        require(not current.is_symlink(), f"Symlinked source: {name}")
    require(current.is_file(), f"Missing source: {name}")
    require(current.resolve().is_relative_to(root.resolve()), f"Source escapes repository: {name}")
    require(current.stat().st_size <= MAX_SOURCE_BYTES, f"Oversized source: {name}")
    return current


def word_paragraphs(raw: bytes) -> list[str]:
    """Read document-order text, including table cells; no Office execution.

    The locator is a paragraph ordinal in word/document.xml, not a page number.
    Drawings/layout, tracked-deletion text and embedded objects aren't interpreted.
    """
    from io import BytesIO

    require(len(raw) <= MAX_SOURCE_BYTES, "Oversized Word source")
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            names = archive.namelist()
            require(len(names) <= 2048 and len(names) == len(set(names)), "Invalid Word members")
            info = archive.getinfo("word/document.xml")
            require(info.file_size <= MAX_XML_BYTES, "Oversized Word document XML")
            xml = archive.read(info)
        require(b"<!DOCTYPE" not in xml.upper() and b"<!ENTITY" not in xml.upper(), "Unsafe XML")
        tree = ET.fromstring(xml)
        return [
            "".join(node.text or "" for node in paragraph.findall(".//w:t", WORD_NS))
            for paragraph in tree.findall(".//w:p", WORD_NS)
        ]
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as error:
        raise ValueError("Invalid Word document source") from error


def documentation_paths(root: Path) -> list[str]:
    """Use tracked paths plus unignored additions, excluding the generated snapshot."""
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        stdout=subprocess.PIPE,
    )
    paths = {name.decode("utf-8") for name in result.stdout.split(b"\0") if name}
    return sorted(
        name
        for name in paths
        if name not in {SNAPSHOT, REVIEW}
        and (name.startswith("docs/") or PurePosixPath(name).suffix.lower() in DOC_SUFFIXES)
    )


def source_record(root: Path, name: str) -> dict[str, Any]:
    raw = safe_source(root, name).read_bytes()
    suffix = PurePosixPath(name).suffix.lower()
    row: dict[str, Any] = {
        "path": name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }
    if suffix in MEDIA_SUFFIXES:
        row.update(kind="media", interpretation="asset_identity_only")
    else:
        if suffix == ".docx":
            text = "\n".join(word_paragraphs(raw))
            kind = "word_document_text"
        else:
            text = raw.decode("utf-8-sig", errors="strict").replace("\r\n", "\n")
            kind = "structured_text" if suffix in {".json", ".csv"} else "narrative_text"
        row.update(
            kind=kind,
            text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            characters=len(text),
            lines=len(text.splitlines()),
            interpretation="source_content_not_runtime_evidence",
        )
    return row


def load_json(path: Path) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            require(key not in result, f"Duplicate JSON field: {key}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)


def source_stages(root: Path, programme: dict[str, Any]) -> list[dict[str, Any]]:
    path = safe_source(root, programme["source"])
    kind = programme["format"]
    require(
        kind
        in {
            "json",
            "word_steps",
            "word_gates",
            "word_rows",
            "word_headings",
            "markdown_table",
            "markdown_headings",
        },
        "Unsupported source extraction format",
    )
    expected_ids = programme["expected_ids"]
    require(
        bool(expected_ids) and len(expected_ids) == len(set(expected_ids)),
        "Invalid expected stage IDs",
    )
    rows = []
    if kind == "json":
        data = load_json(path)
        for key in programme.get("selector", []):
            data = data[key]
        require(type(data) is list, "Stage JSON selection must be a list")
        for index, item in enumerate(data):
            title = item[programme["title_field"]]
            rows.append(
                {
                    "id": item["id"],
                    "title": title,
                    "locator": f"json:{'/'.join(programme.get('selector', []))}/{index}",
                    "source_claim": item.get(
                        "status", item.get("runtime_status", "requirements_only")
                    ),
                    "requirement_sha256": hashlib.sha256(
                        json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                }
            )
    else:
        lines = (
            word_paragraphs(path.read_bytes())
            if kind.startswith("word")
            else path.read_text(encoding="utf-8").splitlines()
        )
        pattern = re.compile(programme["pattern"])
        matches = [(i, pattern.match(line)) for i, line in enumerate(lines)]
        matches = [(i, match) for i, match in matches if match is not None]
        for index, (position, match) in enumerate(matches):
            assert match is not None
            end = matches[index + 1][0] if index + 1 < len(matches) else len(lines)
            block = "\n".join(lines[position:end])
            title = match.groupdict().get("title") or next(
                (line for line in lines[position + 1 : end] if line.strip()), ""
            )
            claim = re.search(r"\bStatus (Completed|Planned)\b", block)
            rows.append(
                {
                    "id": match["id"],
                    "title": title.strip().strip("|").strip(),
                    "locator": f"{'word/document.xml:paragraph' if kind.startswith('word') else 'line'}:{position + 1}",
                    "source_claim": claim[1].lower() if claim else "requirements_only",
                    "requirement_sha256": hashlib.sha256(block.encode("utf-8")).hexdigest(),
                }
            )
    found = [row["id"] for row in rows]
    require(len(found) == len(set(found)), f"Duplicate stage in {programme['namespace']}")
    require(
        set(found) == set(programme["expected_ids"]),
        f"Stage coverage drift: {programme['namespace']}",
    )
    return rows


def owners_for(identity: str, programme: dict[str, Any]) -> list[str]:
    owners = programme.get("owners_by_id", {}).get(identity, programme.get("owners", []))
    require(
        bool(owners)
        and len(owners) == len(set(owners))
        and all(re.fullmatch(r"U(?:0[0-9]|1[0-6])", owner) for owner in owners),
        f"Missing/invalid U owner: {programme['namespace']}:{identity}",
    )
    return sorted(owners)


def narrative_units(root: Path, source: dict[str, Any]) -> list[dict[str, Any]]:
    """Cover all text, not just matches for 'must' or an explicit stage ID.

    Markdown sections include examples/tables as data. Word paragraph ordinals
    include cells. Structured artifacts are covered as a whole, with their
    explicit requirements separately selected by the existing programme policy.
    """
    if source["kind"] == "media":
        return []
    raw = safe_source(root, source["path"]).read_bytes()
    is_word = source["kind"] == "word_document_text"
    lines = word_paragraphs(raw) if is_word else raw.decode("utf-8-sig").splitlines()
    starts = [0]
    fenced = False
    if is_word:
        starts = list(range(len(lines)))
    elif source["kind"] == "narrative_text":
        for index, line in enumerate(lines):
            if line.startswith(("```", "~~~")):
                fenced = not fenced
            if index and not fenced and re.match(r"^#{1,6} ", line):
                starts.append(index)
    units = []
    for start, end in zip(starts, [*starts[1:], len(lines)], strict=True):
        text = "\n".join(lines[start:end])
        if not text.strip():
            continue
        units.append(
            {
                "locator": f"{'paragraph' if is_word else 'line'}:{start + 1}-{end}",
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "excerpt": text.strip()[:160],
            }
        )
    return units


def checked_index(rows: list[dict[str, Any]], field: str, label: str) -> dict[str, Any]:
    result = {row[field]: row for row in rows}
    require(len(result) == len(rows), f"Duplicate {label}")
    return result


def validate_review(root: Path, inventory: dict[str, Any], review: dict[str, Any]) -> None:
    """Validate a separately authored reconciliation; refresh cannot create it.

    This validates traceability and stale-evidence detection, not the truth of a
    reviewer's judgement or the authenticity of arbitrary downloaded CI JSON.
    GitHub URLs identify the independently inspected service records.
    """
    require(review["schema"] == "potbot.roadmap-review/1", "Unknown review schema")
    require(review["reviewed_base"] == inventory["reviewed_base"], "Review base differs")
    require(review["runtime_qualified"] is False, "Review cannot qualify runtime")
    require(review["decision"] == "audit_complete", "Incomplete audit decision")
    families = checked_index(review["families"], "id", "review family")
    for family in families.values():
        require(
            family["status"]
            in {"planned", "partial", "implemented_unqualified", "qualified_subset"},
            "Unknown family disposition",
        )
        require(bool(family["assessment"]) and bool(family["next_action"]), "Empty assessment")
        require(bool(family["owners"]), "Missing review owner")
        require(
            all(re.fullmatch(r"U(?:0[0-9]|1[0-6])", x) for x in family["owners"]),
            "Unknown review owner",
        )
        require(family["remaining"] and family["support_scope"], "Missing evidence boundary")
        for field in ("code", "tests"):
            for evidence in family[field]:
                raw = safe_source(root, evidence["path"]).read_bytes()
                require(
                    hashlib.sha256(raw).hexdigest() == evidence["sha256"],
                    "Stale code/test evidence",
                )
                tree = ast.parse(raw, filename=evidence["path"])
                names = {
                    node.name
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
                }
                require(evidence["symbol"] in names, "Missing code/test symbol")
        if family["status"] != "planned":
            require(family["code"] and family["tests"], "Unsupported implementation disposition")
    sources = checked_index(review["sources"], "path", "source review")
    require(
        set(sources) == {s["path"] for s in inventory["sources"]}, "Unreviewed documentation source"
    )
    narrative = []
    for source in inventory["sources"]:
        disposition = sources[source["path"]]
        require(disposition["sha256"] == source["sha256"], "Stale source review")
        require(
            disposition["assessment"] and disposition["disposition"], "Empty source disposition"
        )
        require(
            disposition["families"] and set(disposition["families"]) <= set(families),
            "Unmapped narrative",
        )
        units = narrative_units(root, source)
        retained_units = disposition["narrative"]
        require(
            [(x["locator"], x["sha256"]) for x in retained_units]
            == [(x["locator"], x["sha256"]) for x in units],
            "Missing or stale narrative review unit",
        )
        for unit, retained in zip(units, retained_units, strict=True):
            require(
                retained["families"] and set(retained["families"]) <= set(families),
                "Unmapped narrative unit",
            )
            narrative.append(
                {"source": source["path"], **unit, "review_families": retained["families"]}
            )
    stages = checked_index(review["requirements"], "key", "stage review")
    require(
        set(stages) == {r["key"] for r in inventory["requirements"]}, "Unreviewed stage requirement"
    )
    for row in inventory["requirements"]:
        disposition = stages[row["key"]]
        require(
            disposition["requirement_sha256"] == row["requirement_sha256"], "Stale stage review"
        )
        require(disposition["assessment"] and disposition["next_action"], "Empty stage assessment")
        require(
            disposition["status"]
            in {"historical", "partial", "planned", "implemented_unqualified", "qualified_subset"},
            "Unknown stage disposition",
        )
        require(
            disposition["families"] and set(disposition["families"]) <= set(families),
            "Unmapped stage",
        )
        selected = [families[x] for x in disposition["families"]]
        if disposition["status"] in {"implemented_unqualified", "qualified_subset"}:
            require(
                any(f["code"] and f["tests"] for f in selected),
                "Stage has no implementation evidence",
            )
        row.update(
            evidence_state="reviewed_" + disposition["status"],
            review_families=disposition["families"],
            reviewed_unified_owners=sorted({o for f in selected for o in f["owners"]}),
            assessment=disposition["assessment"],
            blockers=sorted({b for f in selected for b in f["remaining"]}),
            next_action=disposition["next_action"],
        )
    github = review["github"]
    require(
        github["repository"] == "kieransimkin/potbot" and github["pagination_complete"] is True,
        "Incomplete GitHub inventory",
    )
    require(github["non_pr_issues"] == [], "Issues need explicit requirement dispositions")
    pulls = checked_index(github["pull_requests"], "number", "pull request")
    require(
        set(pulls) == set(range(1, github["last_pr_at_checkpoint"] + 1)),
        "Incomplete pull-request inventory",
    )
    require(
        all(
            p["families"] and set(p["families"]) <= set(families) and p["assessment"]
            for p in pulls.values()
        ),
        "Unmapped pull request",
    )
    require(
        {p["number"] for p in pulls.values() if p["state"] == "open"} == {1, 13},
        "Open-PR review needs renewal",
    )
    require(
        {1, 13} == {p["number"] for p in github["open_pr_sources"]}, "Missing open-PR source review"
    )
    for branch in github["open_pr_sources"]:
        require(
            branch["head_sha"] == pulls[branch["number"]]["head_sha"] and branch["sources"],
            "Unpinned open-PR source",
        )
        for source in branch["sources"]:
            require(
                re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
                and source["families"]
                and set(source["families"]) <= set(families)
                and source["assessment"],
                "Unmapped branch documentation",
            )
        require(
            all(
                x["families"] and set(x["families"]) <= set(families) and x["remaining_gate"]
                for x in branch["additional_requirements"]
            ),
            "Unmapped open-branch requirement",
        )
    checks = github["baseline_checks"]
    require(checks["head_sha"] == inventory["reviewed_base"], "CI belongs to another revision")
    require(
        checks["jobs"]
        and all(
            j["status"] == "completed" and j["conclusion"] == "success" for j in checks["jobs"]
        ),
        "Unsuccessful baseline CI",
    )
    require(
        {j["name"] for j in checks["jobs"]}
        >= {
            "verify",
            "roadmap-inventory",
            "boundaries",
            "windows-launchers",
            "keywordmoves-offline",
            "native-identity (ubuntu-24.04)",
            "native-identity (windows-latest)",
        },
        "Missing baseline CI job",
    )
    require(
        {w["path"] for w in github["workflows"]} == {w["path"] for w in inventory["ci_workflows"]},
        "Unmapped acceptance workflow",
    )
    require(
        {w["path"]: w["sha256"] for w in github["workflows"]}
        == {w["path"]: w["sha256"] for w in inventory["ci_workflows"]},
        "Stale acceptance workflow",
    )
    require(
        all(w["scope"] and w["qualification_limit"] for w in github["workflows"]),
        "Missing workflow evidence boundary",
    )
    require(
        review["inaccessible_sources"]
        and all(
            x["reason"] and x["owners"] and x["next_action"] for x in review["inaccessible_sources"]
        ),
        "Unrecorded inaccessible evidence",
    )
    require(
        review["next_milestone"]["id"] == "U01-A" and review["next_milestone"]["acceptance"],
        "Missing actionable continuation",
    )
    inventory.update(
        scope="U00-A reviewed source, narrative, requirement, GitHub and CI reconciliation; runtime qualification remains separate",
        u00_exit_met=True,
        review_sha256=hashlib.sha256(
            json.dumps(review, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        review_file=REVIEW,
        narrative_coverage=narrative,
        review_families=review["families"],
    )


def build_inventory(
    root: Path, policy: dict[str, Any], *, apply_review: bool = True
) -> dict[str, Any]:
    require(policy["schema"] == "potbot.roadmap-inventory-policy/1", "Unknown inventory policy")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", policy["reviewed_base"])), "Unpinned audit base")
    sources = [source_record(root, name) for name in documentation_paths(root)]
    namespaces = [p["namespace"] for p in policy["programmes"]]
    require(len(namespaces) == len(set(namespaces)), "Duplicate programme namespace")
    require(set(namespaces) == REQUIRED_PROGRAMMES, "Missing or unsupported programme namespace")
    anchor_paths = sorted(
        {
            name
            for route in policy.get("anchor_routes", {}).values()
            for field in ("implementation", "tests")
            for name in route[field]
        }
    )
    anchors = [source_record(root, name) for name in anchor_paths]
    requirements = []
    for programme in policy["programmes"]:
        for row in source_stages(root, programme):
            owners = owners_for(row["id"], programme)
            requirements.append(
                {
                    "key": programme["namespace"] + ":" + row["id"],
                    "legacy_id": row["id"],
                    "source": programme["source"],
                    **row,
                    "unified_owners": owners,
                    "candidate_implementation_anchors": sorted(
                        {
                            name
                            for owner in owners
                            for name in policy.get("anchor_routes", {})
                            .get(owner, {})
                            .get("implementation", [])
                        }
                        | set(programme["implementation_anchors"])
                    ),
                    "candidate_test_anchors": sorted(
                        {
                            name
                            for owner in owners
                            for name in policy.get("anchor_routes", {})
                            .get(owner, {})
                            .get("tests", [])
                        }
                        | set(programme["test_anchors"])
                    ),
                    "evidence_state": "not_qualified_by_inventory",
                    "blockers": [
                        "Requirement-specific implementation and executed-evidence review remains open"
                    ],
                    "next_action": "Reconcile this source claim with current code and exact-revision results under "
                    + ", ".join(owners),
                }
            )
        for name in programme["implementation_anchors"] + programme["test_anchors"]:
            safe_source(root, name)
    keys = [row["key"] for row in requirements]
    require(len(keys) == len(set(keys)), "Duplicate qualified requirement key")
    inventory = {
        "schema": SCHEMA,
        "reviewed_base": policy["reviewed_base"],
        "scope": "U00-A first increment: source and explicit-stage coverage; not full narrative or runtime closure",
        "u00_exit_met": False,
        "runtime_qualified": False,
        "generated_file_excluded_from_own_hash": SNAPSHOT,
        "policy_sha256": hashlib.sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "sources": sources,
        "requirements": sorted(requirements, key=lambda row: row["key"]),
        "candidate_integration_files": anchors,
        "ci_workflows": [
            source_record(root, name)
            for name in sorted(
                p.relative_to(root).as_posix()
                for p in (root / ".github/workflows").glob("*")
                if p.suffix in {".yml", ".yaml"}
            )
        ],
        "open_findings": policy["open_findings"],
    }
    if apply_review and (root / REVIEW).exists():
        validate_review(root, inventory, load_json(safe_source(root, REVIEW)))
    return inventory


def validate_snapshot(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    require(
        actual == expected, "Stale roadmap inventory: review changes, then explicitly --refresh"
    )
    require(actual["runtime_qualified"] is False, "False runtime closure")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh", action="store_true", help="Explicitly regenerate the source inventory"
    )
    args = parser.parse_args()
    policy = load_json(safe_source(ROOT, POLICY))
    expected = build_inventory(ROOT, policy)
    target = ROOT / SNAPSHOT
    if args.refresh:
        require(not target.is_symlink(), "Symlinked inventory output")
        target.write_text(
            json.dumps(expected, indent=2, ensure_ascii=True) + "\n", encoding="utf-8", newline="\n"
        )
    else:
        validate_snapshot(load_json(safe_source(ROOT, SNAPSHOT)), expected)
    print(
        json.dumps(
            {
                "schema": SCHEMA,
                "source_files": len(expected["sources"]),
                "requirements": len(expected["requirements"]),
                "u00_exit_met": expected["u00_exit_met"],
                "runtime_qualified": False,
                "mode": "refresh" if args.refresh else "check",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
