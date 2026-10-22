"""The opt-in hierarchy preserves source definitions and advisory placement semantics."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.domain.syn import SYN_KINDS, SynSnapshot, type_identity
from potbot.semantics import hierarchy
from potbot.semantics.catalog import (
    builtin_types,
    catalog_fingerprint,
    extension_types,
    merge_types,
    type_ancestors,
)

ROOT = Path(__file__).resolve().parents[2]


def test_profile_preserves_all_eighty_authored_aps_definitions_exactly() -> None:
    original = SynSnapshot.from_json(
        (ROOT / "vocab/ai-primitives-0.2.0.snapshot.json").read_text(encoding="utf-8")
    )
    document = hierarchy.profile_document()
    authored = hierarchy.validate_profile(document)
    by_slug = {record.ref.slug: record.to_dict() for record in authored}
    assert len(original.types) == 80
    assert len(authored) == len(by_slug) == 114
    for record in original.types:
        assert by_slug[record.ref.slug] == record.to_dict()
    assert len(set(by_slug) - {record.ref.slug for record in original.types}) == 34


def test_opt_in_profile_keeps_native_and_legacy_graphs_unchanged() -> None:
    original_native = tuple(record.to_dict() for record in builtin_types())
    original_legacy = tuple(record.to_dict() for record in extension_types())
    default_hash = catalog_fingerprint()
    snapshot = hierarchy.profile_snapshot()
    assert snapshot.catalog_mode == "extend_builtin"
    assert snapshot.objects == ()
    assert SynSnapshot.from_json(snapshot.to_json()) == snapshot
    merged = merge_types(snapshot)
    by_slug = {record.ref.slug: record for record in merged}
    assert len(merged) == len(by_slug) == 529 + 47 + 114
    for original in (*original_native, *original_legacy):
        assert by_slug[type_identity(original["ref"])].to_dict() == original
    original_ids = {type_identity(row["ref"]) for row in (*original_native, *original_legacy)}
    authored_ids = {record.ref.slug for record in snapshot.types}
    assert not authored_ids & original_ids
    ancestry = type_ancestors(merged)
    for slug in authored_ids:
        assert "syn://type/thing" in ancestry[str(by_slug[slug].ref)]
    assert tuple(record.to_dict() for record in builtin_types()) == original_native
    assert tuple(record.to_dict() for record in extension_types()) == original_legacy
    assert catalog_fingerprint() == default_hash


@pytest.mark.parametrize(
    "slug",
    ["agent-run-record", "agent-process", "person", "potbot-data-file-agents-md"],
)
def test_type_scheme_aliases_return_identical_cards_and_placement_decisions(slug: str) -> None:
    native = f"syn://type/{slug}"
    preferred = f"synny://type/{slug}"
    assert hierarchy.inspect_hierarchy(type_ref=native) == hierarchy.inspect_hierarchy(
        type_ref=preferred
    )
    assert hierarchy.inspect_placement(native, check_kind="cog") == hierarchy.inspect_placement(
        preferred, check_kind="cog"
    )


@pytest.mark.parametrize(
    "reference",
    [
        "dt:agent.run",
        "agent-run-record",
        "synny://type/agent-run",
        "synny://type/file-agents-md",
        "synny://type/agent-definition-coding",
        "syn://cog/agent-process",
    ],
)
def test_inspection_does_not_invent_compact_or_renamed_type_aliases(reference: str) -> None:
    with pytest.raises(InvalidInputError):
        hierarchy.inspect_hierarchy(type_ref=reference)
    with pytest.raises(InvalidInputError):
        hierarchy.inspect_placement(reference)


def test_execution_records_processes_and_occurrences_have_separate_advisory_placements() -> None:
    expected = {
        "agent-run-record": "ember",
        "agent-process": "cog",
        "agent-execution": "event",
        "workflow-run-record": "ember",
    }
    cards = {}
    for slug, kind in expected.items():
        result = hierarchy.inspect_placement(f"synny://type/{slug}", check_kind=kind)
        assert result["decision"] == "matches_recommendation"
        assert result["type"]["placement"]["preferred_kind"] == kind
        assert result["enforces_import_placement"] is False
        cards[slug] = result["type"]
    assert len({row["ref"] for row in cards.values()}) == len(expected)
    run = cards["agent-run-record"]
    assert "synny://type/agent-process" not in run["ancestors"]
    assert "synny://type/agent-execution" not in run["ancestors"]
    assert all(view["requires_distinct_identity"] for view in run["placement"]["related_views"])
    different = hierarchy.inspect_placement("synny://type/agent-process", check_kind="event")
    assert different["decision"] == "different_from_recommendation"
    assert different["enforces_import_placement"] is False


def test_raw_results_and_objective_assessments_are_not_conflated() -> None:
    expected = {
        "operation-outcome": "ember",
        "execution-trace": "ember",
        "retrieval-result": "ember",
        "verification-assessment": "pondle",
        "benchmark-assessment": "pondle",
        "retrieval-assessment": "pondle",
        "policy-assessment": "pondle",
        "information-need": "enigma",
        "clarification-question": "enigma",
    }
    for slug, kind in expected.items():
        result = hierarchy.inspect_placement(f"synny://type/{slug}")
        assert result["decision"] == "recommendation"
        assert result["type"]["placement"]["preferred_kind"] == kind


def test_native_kinds_and_abstract_placements_do_not_invent_defaults() -> None:
    document = hierarchy.profile_document()
    kinds = [row["kind"] for row in document["primitive_kinds"]]
    assert len(kinds) == len(set(kinds)) == 10
    assert set(kinds) == SYN_KINDS
    placements = {type_identity(row["type_ref"]): row for row in document["placements"]}
    abstract_count = 0
    for row in placements.values():
        if row["abstract"]:
            abstract_count += 1
            assert row["preferred_kind"] is None
            inspected = hierarchy.inspect_placement(row["type_ref"], check_kind="thing")
            assert inspected["decision"] == "abstract"
    assert abstract_count > 0
    original = SynSnapshot.from_json(
        (ROOT / "vocab/ai-primitives-0.2.0.snapshot.json").read_text(encoding="utf-8")
    )
    for record in original.types:
        abstract = json.loads(record.metadata_json)["authored_type_definition"]["abstract"]
        assert placements[record.ref.slug]["abstract"] is abstract
    for slug in ("agents-md-file", "file-occurrence", "opaque-bytes", "syn-type-definition"):
        result = hierarchy.inspect_placement(f"synny://type/{slug}")
        assert result["type"]["placement"]["preferred_kind"] not in {"snug", "om", "type"}
    person = hierarchy.inspect_placement("syn://type/person", check_kind="thing")
    assert person["decision"] == "no_recommendation"
    assert person["type"]["placement"] is None
    assert "syn://type/thing" in person["type"]["ancestors"]
    budget = hierarchy.inspect_placement("synny://type/resource-budget-record", check_kind="om")
    assert budget["type"]["placement"]["preferred_kind"] == "ember"
    assert budget["decision"] == "different_from_recommendation"
    with pytest.raises(InvalidInputError):
        hierarchy.inspect_hierarchy(primitive_kind="artifact")


def test_hierarchy_pages_are_exhaustive_unique_and_ordered_by_type_identity() -> None:
    expected = sorted(record.ref.slug for record in merge_types(hierarchy.profile_snapshot()))
    collected = []
    offset = 0
    profile_hash = hierarchy.profile_hash()
    while True:
        page = hierarchy.inspect_hierarchy(limit=37, offset=offset)
        assert page["profile_hash"] == profile_hash
        assert page["selected_count"] == len(expected) == 690
        assert page["returned_count"] == len(page["types"]) <= 37
        collected.extend(type_identity(row["ref"]) for row in page["types"])
        if not page["has_more"]:
            assert page["next_offset"] is None
            break
        assert page["next_offset"] == offset + page["returned_count"]
        offset = page["next_offset"]
    assert collected == expected
    assert len(set(collected)) == len(collected)
    exhausted = hierarchy.inspect_hierarchy(offset=len(expected), limit=37)
    assert exhausted["types"] == [] and exhausted["next_offset"] is None
    summary = hierarchy.inspect_hierarchy(summary=True)
    assert summary["selected_count"] == len(expected)
    assert summary["returned_count"] == 0 and "types" not in summary


def test_subtype_and_primitive_filters_select_explicit_recommendations() -> None:
    parent = "synny://type/execution-record"
    exact = hierarchy.inspect_hierarchy(type_ref=parent)
    descendants = hierarchy.inspect_hierarchy(type_ref=parent, include_subtypes=True, limit=1000)
    assert exact["selected_count"] == 1
    assert descendants["selected_count"] > 1
    assert len({type_identity(row["ref"]) for row in descendants["types"]}) == len(
        descendants["types"]
    )
    recommended = hierarchy.inspect_hierarchy(
        type_ref="syn://type/execution-record",
        include_subtypes=True,
        primitive_kind="ember",
        limit=1000,
    )
    expected = [
        row for row in descendants["types"] if row["placement"]["preferred_kind"] == "ember"
    ]
    assert recommended["types"] == expected
    assert "agent-run-record" in {type_identity(row["ref"]) for row in recommended["types"]}
    assert recommended["primitive_filter_semantics"] == "explicit_advisory_recommendation"
    assert recommended["enforces_import_placement"] is False


def test_profile_and_type_cards_do_not_expose_mutable_fingerprint_cache() -> None:
    original = hierarchy.profile_document()
    original_hash = hierarchy.profile_hash()
    detached = hierarchy.profile_document()
    detached["types"][0]["name"] = "changed outside the registry"
    detached["placements"][0]["rationale"] = "changed outside the registry"
    detached["relations"].clear()
    before = hierarchy.inspect_placement("synny://type/agent-run-record")
    expected = deepcopy(before)
    before["type"]["parents"].append("synny://type/not-a-member")
    before["type"]["placement"]["rationale"] = "mutated returned card"
    assert hierarchy.profile_document() == original
    assert hierarchy.profile_hash() == original_hash
    fresh = hierarchy.inspect_placement("syn://type/agent-run-record")
    assert fresh == expected


@pytest.mark.parametrize(
    "problem",
    [
        "duplicate_type_alias",
        "missing_parent",
        "cycle",
        "missing_placement",
        "duplicate_placement_alias",
        "bad_preferred_kind",
        "unhashable_primitive_kind",
        "boolean_schema_version",
        "abstract_concrete_default",
        "native_replacement",
        "protected_replacement",
    ],
)
def test_malformed_profiles_fail_as_domain_errors(problem: str) -> None:
    document = hierarchy.profile_document()
    first = document["types"][0]
    if problem == "duplicate_type_alias":
        duplicate = deepcopy(first)
        duplicate["ref"] = "syn://type/" + type_identity(first["ref"])
        document["types"].append(duplicate)
    elif problem == "missing_parent":
        first["parents"].append("syn://type/nonexistent-profile-parent")
    elif problem == "cycle":
        second = document["types"][1]
        first["parents"] = [second["ref"]]
        second["parents"] = [first["ref"]]
    elif problem == "missing_placement":
        document["placements"] = [
            row
            for row in document["placements"]
            if type_identity(row["type_ref"]) != type_identity(first["ref"])
        ]
    elif problem == "duplicate_placement_alias":
        duplicate = deepcopy(document["placements"][0])
        duplicate["type_ref"] = "syn://type/" + type_identity(duplicate["type_ref"])
        document["placements"].append(duplicate)
    elif problem == "bad_preferred_kind":
        document["placements"][0]["preferred_kind"] = "artifact"
    elif problem == "unhashable_primitive_kind":
        document["primitive_kinds"][0]["kind"] = []
    elif problem == "boolean_schema_version":
        document["schema_version"] = True
    elif problem == "abstract_concrete_default":
        document["placements"][0].update(abstract=True, preferred_kind="thing")
    elif problem == "native_replacement":
        document["types"].append(builtin_types()[0].to_dict())
    elif problem == "protected_replacement":
        document["types"].append(extension_types()[0].to_dict())
    else:
        raise AssertionError(problem)
    with pytest.raises(InvalidInputError):
        hierarchy.validate_profile(document)
