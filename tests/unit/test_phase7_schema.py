"""Versioned SQL graph contracts and URL-only Syn declarations."""

import json
from pathlib import Path

from potbot.persistence.schema import application_migrations


def test_research_migration_is_versioned_and_immutably_scoped():
    revisions = application_migrations()
    assert revisions[15].version == 16
    assert revisions[15].name == "phase7_schema"
    assert tuple(r.version for r in revisions[:18]) == tuple(range(1, 19))
    sql = "\n".join(revisions[15].statements)
    assert "FOREIGN KEY(workspace_id,episode_id,need_id)" in sql
    assert "FOREIGN KEY(workspace_id,need_id,need_revision_id)" in sql
    assert "FOREIGN KEY(workspace_id,episode_id,predecessor_id)" in sql
    assert "source_artifact_id uuid NOT NULL" in sql
    assert set(revisions[15].append_only_tables) == {
        "research_actions",
        "research_observations",
    }


def test_phase7_types_are_explicit_and_form_a_parent_tree():
    source = Path(__file__).resolve().parents[2] / "vocab" / "phase7-research-types.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    refs = {entry["ref"] for entry in document["types"]}
    assert len(refs) == len(document["types"])
    for entry in document["types"]:
        assert entry["ref"].startswith(("syn://type/", "synny://type/"))
        assert entry["parents"]
        assert all(
            parent.startswith(("syn://type/", "synny://type/")) for parent in entry["parents"]
        )
        for parent in entry["parents"]:
            assert parent.startswith("syn://type/") or parent in refs
    for cog in document["cogs"]:
        assert cog["ref"].startswith("syn://cog/")
        assert cog["inputTypeRef"] in refs and cog["outputTypeRef"] in refs
    assert document["classification"].startswith("pending-syn-registry-import")
