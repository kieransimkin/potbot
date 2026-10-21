"""Native semantic classes, representation aliases and snapshot DAG boundaries."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.domain.syn import SynnyRef, SynProperty, SynRef, SynSnapshot, SynType
from potbot.semantics import catalog

ROOT = Path(__file__).resolve().parents[2]


def definition(slug: str, *parents: str, name: str | None = None) -> SynType:
    return SynType(
        SynRef(f"syn://type/{slug}"),
        name or slug,
        tuple(SynRef(f"syn://type/{parent}") for parent in parents),
        None,
        (),
    )


def snapshot(*types: SynType, mode: str = "complete") -> SynSnapshot:
    return SynSnapshot("unit-fixture", mode, types, ())


def test_static_native_catalog_preserves_polyhierarchy_and_source_versions() -> None:
    records = catalog.builtin_types()
    assert len(records) == 529
    assert sum(len(record.parents) for record in records) == 859
    assert sum(len(record.parents) > 1 for record in records) == 183
    assert len({record.ref for record in records}) == 529
    by_ref = {record.ref: record for record in records}
    ancestors = catalog.type_ancestors(records)
    company = ancestors["syn://type/company"]
    assert {"syn://type/organization", "syn://type/legal-entity", "syn://type/agent"} <= set(
        company
    )
    assert "syn://type/animal" not in company
    assert by_ref["syn://type/computational-procedure"].parents == (
        "syn://type/computational-method",
    )
    assert sum(len(refs) for refs in ancestors.values()) == 3377
    assert "any other kind of response" in (by_ref["syn://type/question"].definition or "")
    assert by_ref["syn://type/artifact"].parents == ("syn://type/material-thing",)
    assert by_ref["syn://type/cog"].parents == ("syn://type/computational-process",)
    assert "syn://type/information-object" in ancestors["syn://type/crypto-price-feed"]
    provenance = catalog.catalog_provenance()
    assert [
        (row["ontology"], row["version"], row["type_count"]) for row in provenance["native_sources"]
    ] == [
        ("syn-core", 4, 369),
        ("syn-feeds", 1, 81),
    ]
    assert all(len(row["sha256"]) == 64 for row in provenance["contract_sources"])


def test_database_catalog_is_complete_and_original_seed_lineage_remains_reproducible() -> None:
    source_bytes = (ROOT / "vocab/syn-database-types.snapshot.json").read_bytes()
    source = SynSnapshot.from_json(source_bytes.decode())
    extraction = json.loads((ROOT / "vocab/syn-database-types.provenance.json").read_text())
    assert source.catalog_mode == "complete"
    assert source.objects == ()
    assert source.types == catalog.builtin_types()
    assert extraction["type_refs"] == [record.ref for record in source.types]
    assert extraction["snapshot_sha256"] == hashlib.sha256(source_bytes).hexdigest()
    assert extraction["source_sha256"] == (
        "2f4d949fb5b2996ee88d2dbcbbbe6996cdd7213d1c4aef50b26fad4857a1ab3e"
    )
    assert extraction["sql_executed"] is False
    assert extraction["direct_parent_edges"] == 859
    assert extraction["direct_property_declarations"] == 0
    bundle = json.loads((ROOT / "src/potbot/semantics/catalog.json").read_text())
    seed = bundle["seed_types"]
    provenance = catalog.catalog_provenance()
    assert len(seed) == 450
    assert sum(len(record["parents"]) for record in seed) == 558
    assert "definitions" not in provenance["seed_registry"]
    assert provenance["seed_registry"]["definitions_resource"].endswith("catalog.json#/seed_types")
    encoded_seed = json.dumps(seed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert (
        provenance["seed_registry"]["definitions_sha256"]
        == hashlib.sha256(encoded_seed.encode()).hexdigest()
    )
    seed_by_ref = {record["ref"]: record for record in seed}
    assert set(seed_by_ref) <= {record.ref for record in source.types}
    assert seed_by_ref["syn://type/computational-procedure"]["parents"] == [
        "syn://type/procedure",
        "syn://type/computational-method",
    ]
    reconciliation = provenance["reconciliation"]
    assert reconciliation["overlap_count"] == 450
    assert len(reconciliation["database_only_refs"]) == 79
    assert reconciliation["seed_only_refs"] == []
    assert reconciliation["changed_parent_refs"] == ["syn://type/computational-procedure"]
    assert reconciliation["changed_name_refs"] == []
    assert reconciliation["changed_definition_refs"] == []


def test_every_existing_data_definition_is_an_explicit_unchanged_alias() -> None:
    source_bytes = (ROOT / "vocab/data-types.json").read_bytes()
    original = json.loads(source_bytes)
    assert len(original["types"]) == 45
    extensions = catalog.extension_types()
    assert len(extensions) == 47
    by_ref = {record.ref: record for record in extensions}
    crosswalk = json.loads((ROOT / "vocab/syn-typing-crosswalk.json").read_text())
    mapped = {row["data_type_id"]: row for row in crosswalk["aliases"]}
    for original_type in original["types"]:
        reference = catalog.data_type_ref(original_type["id"])
        record = by_ref[reference]
        assert mapped[original_type["id"]]["syn_ref"] == reference
        assert mapped[original_type["id"]]["original_definition"] == original_type
        assert json.loads(record.metadata_json)["data_type"] == original_type
        assert record.definition == original_type["definition"]
        assert record.parents == tuple(
            catalog.data_type_ref(parent) for parent in original_type["is_a"]
        )
        assert catalog.data_type_ref(str(reference)) == reference
        alternate = str(reference).replace("synny://", "syn://", 1)
        assert catalog.data_type_ref(alternate) == reference
        assert mapped[original_type["id"]]["equivalent_type_refs"] == [reference, alternate]
        assert isinstance(reference, SynnyRef)
        assert reference.startswith("synny://type/potbot-data-")
    assert by_ref[catalog.data_type_ref("dt:data")].parents == ()
    for record_type in ("dt:syn.object_record", "dt:syn.type_definition"):
        assert by_ref[catalog.data_type_ref(record_type)].parents == (
            catalog.data_type_ref("dt:record"),
        )
    assert (
        catalog.catalog_provenance()["data_type_source"]["sha256"]
        == hashlib.sha256(source_bytes).hexdigest()
    )


def test_definition_run_and_native_semantic_branches_remain_distinct() -> None:
    ancestors = catalog.type_ancestors((*catalog.builtin_types(), *catalog.extension_types()))
    assert (
        catalog.data_type_ref("dt:agent.definition")
        in ancestors[catalog.data_type_ref("dt:agent.definition.coding")]
    )
    for family in ("agent", "workflow"):
        assert (
            catalog.data_type_ref(f"dt:{family}.definition")
            not in ancestors[catalog.data_type_ref(f"dt:{family}.run")]
        )
        assert "syn://type/thing" not in ancestors[catalog.data_type_ref(f"dt:{family}")]
    assert (
        catalog.data_type_ref("dt:value.number")
        not in ancestors[catalog.data_type_ref("dt:value.boolean")]
    )


@pytest.mark.parametrize(
    "value",
    ["dt:not_registered", "syn://type/person", "synny://type/person", "dt:File", " dt:file"],
)
def test_alias_lookup_never_invents_or_normalizes_an_alias(value: str) -> None:
    with pytest.raises(InvalidInputError, match="unknown"):
        catalog.data_type_ref(value)


def test_complete_catalog_does_not_reintroduce_seed_types_or_parents() -> None:
    supplied = definition("changed-root", name="A native local label")
    merged = catalog.merge_types(snapshot(supplied))
    refs = {record.ref for record in merged}
    assert len(merged) == 48
    assert supplied.ref in refs
    assert "syn://type/thing" not in refs
    assert "syn://type/person" not in refs
    assert merged == tuple(sorted(merged, key=lambda record: record.ref))


def test_extended_catalog_replaces_the_entire_matching_native_definition() -> None:
    changed = definition("person", "information-object", name="Edited source class")
    merged = catalog.merge_types(snapshot(changed, mode="extend_builtin"))
    assert len(merged) == 576
    result = next(record for record in merged if record.ref == changed.ref)
    assert result == changed
    assert "syn://type/agent" not in catalog.type_ancestors(merged)[str(changed.ref)]
    assert "syn://type/human-organism" not in catalog.type_ancestors(merged)[str(changed.ref)]


@pytest.mark.parametrize("mode", ["complete", "extend_builtin"])
@pytest.mark.parametrize("slug", ["potbot-data-file", "potbot-data-new-class"])
@pytest.mark.parametrize("scheme", ["syn", "synny"])
def test_snapshot_cannot_supply_any_protected_data_alias(mode: str, slug: str, scheme: str) -> None:
    supplied = definition(slug)
    if scheme == "synny":
        supplied = replace(supplied, ref=SynnyRef(f"synny://type/{slug}"))
    with pytest.raises(InvalidInputError, match="protected"):
        catalog.merge_types(snapshot(supplied, mode=mode))


def test_type_scheme_aliases_normalize_graph_edges_ranges_and_fingerprints() -> None:
    a = replace(definition("a"), ref=SynnyRef("synny://type/a"))
    b = replace(
        definition("b"),
        parents=(SynnyRef("synny://type/a"),),
        properties=(SynProperty("related", SynnyRef("synny://type/a"), 0, 3),),
        metadata_json='{"source_spelling":"synny://type/b"}',
    )
    merged = catalog.merge_types(snapshot(a, b))
    by_ref = {record.ref: record for record in merged}
    assert len(merged) == 49
    assert "synny://type/a" not in by_ref
    assert by_ref["syn://type/a"] == definition("a")
    assert by_ref["syn://type/b"].parents == (SynRef("syn://type/a"),)
    assert by_ref["syn://type/b"].properties[0].type_ref == SynRef("syn://type/a")
    assert by_ref["syn://type/b"].metadata_json == b.metadata_json
    assert catalog.type_ancestors((a, b))["syn://type/b"] == ("syn://type/a", "syn://type/b")
    assert catalog.catalog_fingerprint((a, b)) == catalog.catalog_fingerprint(
        (by_ref["syn://type/a"], by_ref["syn://type/b"])
    )
    assert catalog.canonical_type_ref("synny://type/person") == SynRef("syn://type/person")
    assert catalog.canonical_type_ref("syn://type/potbot-data-file") == SynnyRef(
        "synny://type/potbot-data-file"
    )
    with pytest.raises(InvalidInputError, match="type"):
        catalog.canonical_type_ref("syn://thing/person")


def test_native_alias_definition_overrides_one_existing_class() -> None:
    changed = replace(
        definition("person", "information-object", name="Changed once"),
        ref=SynnyRef("synny://type/person"),
    )
    merged = catalog.merge_types(snapshot(changed, mode="extend_builtin"))
    assert len(merged) == 576
    assert len({record.ref.slug for record in merged}) == 576
    result = next(record for record in merged if record.ref.slug == "person")
    assert result == replace(changed, ref=SynRef("syn://type/person"))


def test_aliases_cannot_declare_duplicate_classes_or_parent_memberships() -> None:
    native = definition("person")
    alias = replace(native, ref=SynnyRef("synny://type/person"))
    with pytest.raises(InvalidInputError, match="duplicate"):
        snapshot(native, alias)
    with pytest.raises(InvalidInputError, match="duplicate"):
        catalog.type_ancestors((native, alias))
    with pytest.raises(InvalidInputError, match="duplicate"):
        replace(
            definition("child"),
            parents=(SynRef("syn://type/person"), SynnyRef("synny://type/person")),
        )


def test_missing_parents_and_property_ranges_fail_after_merge() -> None:
    with pytest.raises(InvalidInputError, match="missing SYN parent"):
        catalog.merge_types(snapshot(definition("custom", "absent")))
    with pytest.raises(InvalidInputError, match="missing SYN property range"):
        catalog.merge_types(
            snapshot(
                replace(
                    definition("custom"),
                    properties=(SynProperty("friend", SynRef("syn://type/absent"), 0, 3),),
                )
            )
        )


def test_display_labels_may_repeat_and_redundant_edges_do_not_duplicate_membership() -> None:
    a = definition("a", name="Same label")
    b = definition("b", "a", name="Same label")
    c = definition("c", "b", "a")
    merged = catalog.merge_types(snapshot(a, b, c))
    ancestors = catalog.type_ancestors(merged)
    assert ancestors[str(c.ref)] == (str(a.ref), str(b.ref), str(c.ref))


@pytest.mark.parametrize(
    "types", [(definition("a", "a"),), (definition("a", "b"), definition("b", "a"))]
)
def test_cycles_fail_without_recursive_traversal(types: tuple[SynType, ...]) -> None:
    with pytest.raises(InvalidInputError, match="cycle"):
        catalog.merge_types(snapshot(*types))


def test_large_deep_graph_fails_the_pair_bound_without_stack_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog, "MAX_SYN_ANCESTRY_PAIRS", 1000)
    chain = tuple(definition(f"node-{i}", *([f"node-{i - 1}"] if i else [])) for i in range(1200))
    merged = catalog.merge_types(snapshot(*chain))
    with pytest.raises(InvalidInputError, match="unique pairs"):
        catalog.type_ancestors(merged)


def test_effective_catalog_count_includes_protected_extensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog, "MAX_SYN_TYPES", 48)
    with pytest.raises(InvalidInputError, match="merged SYN catalog exceeds"):
        catalog.merge_types(snapshot(definition("a"), definition("b")))


def test_fingerprint_pins_effective_meaning_and_provenance_without_mutable_cache_leaks() -> None:
    merged = catalog.merge_types(snapshot(definition("a")))
    assert catalog.catalog_fingerprint(merged) == catalog.catalog_fingerprint(reversed(merged))
    changed = catalog.merge_types(
        snapshot(replace(definition("a"), metadata_json='{"revision":2}'))
    )
    assert catalog.catalog_fingerprint(merged) != catalog.catalog_fingerprint(changed)
    copied = catalog.catalog_provenance()
    copied["native_sources"][0]["version"] = "mutated"
    assert catalog.catalog_provenance()["native_sources"][0]["version"] == 4
