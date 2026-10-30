"""An opt-in catalog must preserve every historical protected data alias."""
import json
from pathlib import Path

from potbot.context.syn_release import context_syn_catalog, context_syn_types
from potbot.semantics import catalog

ROOT = Path(__file__).resolve().parents[1]


def test_release_matches_declared_exact_roles():
    manifest = json.loads((ROOT / "vocab/context-document-types-1.0.0.json").read_text())
    records = {str(row.ref): row for row in context_syn_types()}
    assert len(records) == 3
    assert set(records) == {row["ref"] for row in manifest["types"]}
    assert all(record.parents == (catalog.data_type_ref("dt:file"),) for record in records.values())


def test_opt_in_catalog_keeps_all_pinned_data_aliases():
    merged = {str(row.ref): row for row in context_syn_catalog()}
    original = catalog.extension_types()
    assert all(merged[str(row.ref)] == row for row in original)
    assert len(merged) == len(catalog.builtin_types()) + len(original) + 3
