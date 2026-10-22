#!/usr/bin/env python3
"""Regenerate or check the opt-in hierarchy export, reference, and synthetic example."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema

from potbot.domain.syn import SynObject, SynRef, SynSnapshot, parse_type_ref, type_identity
from potbot.semantics.catalog import (
    builtin_types,
    catalog_fingerprint,
    extension_types,
    merge_types,
    type_ancestors,
)
from potbot.semantics.hierarchy import (
    inspect_hierarchy,
    profile_document,
    profile_hash,
    profile_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
START = "<!-- BEGIN GENERATED TYPE TABLE -->"
END = "<!-- END GENERATED TYPE TABLE -->"


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def repository_text(path: Path) -> bytes:
    """Match Git text content across LF and CRLF checkouts without rewriting input."""
    return path.read_text(encoding="utf-8").encode("utf-8")


def example(snapshot: SynSnapshot) -> tuple[SynSnapshot, dict[str, Any]]:
    # These public records illustrate identity and placement, not APS payload admission.
    rows = [
        ("ember", "definition", "coding-agent-definition", "Coding agent definition"),
        ("thing", "agent", "software-agent", "Software agent"),
        ("cog", "process", "agent-process", "Agent computational process"),
        ("event", "execution", "agent-execution", "Agent execution occurrence"),
        ("ember", "run-record", "agent-run-record", "Record of that execution"),
        ("enigma", "question", "verification-question", "Does the file meet the criterion?"),
        ("ember", "outcome", "operation-outcome", "Recorded operation outcome"),
        ("thing", "file", "file-occurrence", "A source file occurrence"),
        ("ember", "criterion", "evaluation-criterion-definition", "Evaluation criterion"),
        ("pondle", "assessment", "verification-assessment", "Objective assessment record"),
        ("ember", "preference", "preference-record", "Recorded formatting preference"),
        ("ember", "budget", "resource-budget-record", "Recorded token allowance"),
    ]
    preferred = {row.ref.slug: str(row.ref) for row in snapshot.types}
    objects = []
    references: dict[str, str] = {}
    for kind, slug, class_slug, name in rows:
        ref = SynRef(f"syn://{kind}/example-{slug}")
        references[slug] = str(ref)
        type_ref = parse_type_ref(preferred.get(class_slug, f"syn://type/{class_slug}"))
        payload = {
            "id": str(ref),
            "typeRef": str(type_ref),
            "name": f"Synthetic example: {name}",
            "knowledge": {
                "synthetic": True,
                "purpose": "Illustrate semantic identity and native primitive placement only.",
                "payload_status": "No APS payload profile or live execution is claimed.",
            },
        }
        objects.append(SynObject(ref, type_ref, json.dumps(payload, ensure_ascii=False)))
    result = SynSnapshot(
        "Synthetic Potbot primitive views; no live execution or APS admission",
        "extend_builtin",
        snapshot.types,
        tuple(objects),
    )
    links = [
        ("definition", "defines", "agent"),
        ("process", "uses-definition", "definition"),
        ("execution", "uses-definition", "definition"),
        ("execution", "part-of-process", "process"),
        ("execution", "produces", "outcome"),
        ("run-record", "records", "execution"),
        ("run-record", "about", "agent"),
        ("outcome", "produced-by", "execution"),
        ("assessment", "evaluates", "file"),
        ("assessment", "uses-definition", "criterion"),
        ("assessment", "answers", "question"),
        ("preference", "about", "agent"),
        ("budget", "about", "process"),
    ]
    document = {
        "schema_version": 1,
        "profile_id": profile_document()["profile_id"],
        "profile_hash": profile_hash(),
        "snapshot_fingerprint": result.fingerprint,
        "synthetic": True,
        "status": "illustrative_only_not_imported_by_syn_snapshot_service",
        "links": [
            {
                "source_ref": references[source],
                "relation": relation,
                "target_ref": references[target],
            }
            for source, relation, target in links
        ],
    }
    # Prevent the example from drifting outside the explicitly documented relation scopes.
    ancestry = type_ancestors(merge_types(result))
    ancestors_by_id = {
        type_identity(ref): {type_identity(parent) for parent in parents}
        for ref, parents in ancestry.items()
    }
    by_ref = {str(row.ref): row for row in result.objects}
    relations = {row["name"]: row for row in profile_document()["relations"]}
    for link in document["links"]:
        relation = relations[link["relation"]]
        assert link["source_ref"] != link["target_ref"]
        for field, side in (("source_ref", "domains"), ("target_ref", "ranges")):
            obj = by_ref[link[field]]
            assert any(
                obj.ref.kind in endpoint["allowed_kinds"]
                and type_identity(endpoint["type_ref"]) in ancestors_by_id[obj.type_ref.slug]
                for endpoint in relation[side]
            ), (link, side)
    return result, document


def generated_assets() -> dict[Path, bytes]:
    document = profile_document()
    schema = json.loads(repository_text(ROOT / "vocab/potbot-hierarchy-profile.schema.json"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(document)
    snapshot = profile_snapshot()
    original_path = ROOT / "vocab/ai-primitives-0.2.0.snapshot.json"
    original = SynSnapshot.from_json(original_path.read_text(encoding="utf-8"))
    original_by_id = {row.ref.slug: row.to_dict() for row in original.types}
    authored_by_id = {row.ref.slug: row.to_dict() for row in snapshot.types}
    assert len(original_by_id) == 80
    assert all(authored_by_id[key] == row for key, row in original_by_id.items())
    merged = merge_types(snapshot)
    ancestry = type_ancestors(merged)
    snapshot_bytes = json_bytes(snapshot.to_dict())
    example_snapshot, links = example(snapshot)
    snapshot_schema = json.loads(repository_text(ROOT / "vocab/syn-snapshot.schema.json"))
    validator = jsonschema.Draft202012Validator(snapshot_schema)
    validator.validate(snapshot.to_dict())
    validator.validate(example_snapshot.to_dict())
    provenance = {
        "schema_version": 1,
        "profile_id": document["profile_id"],
        "profile_status": document["status"],
        "profile_hash": profile_hash(),
        "profile_file": "src/potbot/semantics/hierarchy_profile.json",
        "profile_file_sha256": digest(
            repository_text(ROOT / "src/potbot/semantics/hierarchy_profile.json")
        ),
        "repository_text_hash_policy": "UTF-8 with CRLF normalized to LF; archived source hashes retain their original byte meaning.",
        "source_aps_archive": {
            "name": "AI-Primitive-Specifications-0.2.0-draft.zip",
            "sha256": "97c1cf0dadbef222315cca6f1662ee651a93a44f04c4f94f82a05f242be421b9",
        },
        "source_aps_snapshot_sha256": digest(repository_text(original_path)),
        "explicit_type_migration_file": "vocab/ai-primitives-type-migration.json",
        "explicit_type_migration_sha256": digest(
            repository_text(ROOT / "vocab/ai-primitives-type-migration.json")
        ),
        "native_and_protected_catalog_sha256": digest(
            repository_text(ROOT / "src/potbot/semantics/catalog.json")
        ),
        "export_file": "vocab/potbot-hierarchy.snapshot.json",
        "export_file_sha256": digest(snapshot_bytes),
        "export_fingerprint": snapshot.fingerprint,
        "effective_catalog_hash": catalog_fingerprint(merged),
        "counts": {
            "native": len(builtin_types()),
            "protected_legacy": len(extension_types()),
            "preserved_aps_authored": len(original.types),
            "new_authored": len(snapshot.types) - len(original.types),
            "authored": len(snapshot.types),
            "effective": len(merged),
            "effective_direct_parent_edges": sum(len(row.parents) for row in merged),
            "effective_ancestor_pairs_including_self": sum(
                len(parents) for parents in ancestry.values()
            ),
            "explicit_placement_decisions": len(document["placements"]),
            "native_reuse_decisions": len(document["reuse_decisions"]),
            "objects": 0,
        },
        "equivalence": "Same-slug syn://type/ and synny://type/ references count once.",
        "placement_enforcement": "Advisory only; existing imports and payload validation unchanged.",
        "regenerate": "uv run --locked python scripts/generate_type_hierarchy.py",
        "check": "uv run --locked python scripts/generate_type_hierarchy.py --check",
    }
    reference = ROOT / "docs/type-hierarchy-reference.md"
    text = reference.read_text(encoding="utf-8")
    assert text.count(START) == text.count(END) == 1
    before, rest = text.split(START)
    _, after = rest.split(END)
    cards = inspect_hierarchy(limit=1000)["types"]
    table = [
        "| Preferred Type reference | Source layer | Direct parents | Profile abstraction | Recommended primitive kind |",
        "| --- | --- | --- | --- | --- |",
    ]
    for card in cards:
        if card["origin"] != "authored_profile":
            continue
        layer = (
            "APS 0.2.0 (preserved)"
            if type_identity(card["ref"]) in original_by_id
            else "Potbot 0.1.0"
        )
        parents = ", ".join(f"`{parent}`" for parent in card["parents"])
        abstract = "abstract" if card["abstract"] else "concrete"
        kind = card["placement"]["preferred_kind"]
        kind_label = "none (abstract)" if kind is None else f"`{kind}`"
        table.append(f"| `{card['ref']}` | {layer} | {parents} | {abstract} | {kind_label} |")
    generated_reference = before + START + "\n\n" + "\n".join(table) + "\n\n" + END + after
    return {
        ROOT / "vocab/potbot-hierarchy.snapshot.json": snapshot_bytes,
        ROOT / "vocab/potbot-hierarchy.provenance.json": json_bytes(provenance),
        ROOT / "vocab/example-potbot-primitive-views.snapshot.json": json_bytes(
            example_snapshot.to_dict()
        ),
        ROOT / "vocab/example-potbot-primitive-links.json": json_bytes(links),
        reference: generated_reference.encode("utf-8"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if generated assets differ")
    args = parser.parse_args()
    stale = []
    for path, content in generated_assets().items():
        if args.check:
            if not path.exists() or repository_text(path) != content:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.write_bytes(content)
    if stale:
        print("Out-of-date hierarchy assets: " + ", ".join(stale))
        return 1
    print("Hierarchy assets checked." if args.check else "Hierarchy assets regenerated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
