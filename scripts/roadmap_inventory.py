#!/usr/bin/env python3
"""U00-A source inventory and namespaced stage crosswalk, never runtime qualification.

Read repository documentation without importing it, executing examples, following
external references, or fetching anything. Refresh is explicit; check is read-only.
"""

from __future__ import annotations

import argparse
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
        if name != SNAPSHOT
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


def build_inventory(root: Path, policy: dict[str, Any]) -> dict[str, Any]:
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
    return {
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


def validate_snapshot(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    require(
        actual == expected, "Stale roadmap inventory: review changes, then explicitly --refresh"
    )
    require(
        actual["u00_exit_met"] is False and actual["runtime_qualified"] is False, "False closure"
    )


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
                "u00_exit_met": False,
                "runtime_qualified": False,
                "mode": "refresh" if args.refresh else "check",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
