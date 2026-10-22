"""T01.1 — inventory is read-only, source-bound, and collision-aware."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.tool_platform_inventory import ANCHORS, EXPECTED_BASELINE, build_inventory


def seed(root: Path) -> None:
    for anchor in ANCHORS:
        if anchor.decision == "missing":
            continue
        path = root / anchor.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture:{anchor.component}\n", encoding="utf-8")


def test_inventory_has_explicit_integrators_and_source_digests(tmp_path: Path) -> None:
    seed(tmp_path)
    report = build_inventory(tmp_path)
    assert report["stage_id"] == "T01.1"
    assert report["planning_baseline"] == EXPECTED_BASELINE
    assert report["missing_required_anchors"] == []
    assert report["unexpected_preexisting_components"] == []
    assert len(report["components"]) == len(ANCHORS)
    assert all(row["qualification"] == "not_established_by_inventory" for row in report["components"])
    assert all(row["sha256"] is not None for row in report["components"] if row["presence"] == "present")
    assert build_inventory(tmp_path)["inventory_sha256"] == report["inventory_sha256"]
    assert all(row["owner"] for row in report["components"])


def test_inventory_hash_changes_when_source_changes(tmp_path: Path) -> None:
    seed(tmp_path)
    before = build_inventory(tmp_path)["inventory_sha256"]
    path = tmp_path / ANCHORS[0].path
    path.write_text("changed fixture", encoding="utf-8")
    assert build_inventory(tmp_path)["inventory_sha256"] != before


def test_inventory_does_not_silently_accept_missing_existing_code(tmp_path: Path) -> None:
    seed(tmp_path)
    (tmp_path / ANCHORS[0].path).unlink()
    report = build_inventory(tmp_path)
    assert ANCHORS[0].path in report["missing_required_anchors"]


def test_inventory_flags_new_existing_mcp_adapter_for_review(tmp_path: Path) -> None:
    seed(tmp_path)
    target = next(row for row in ANCHORS if row.component == "mcp_adapter")
    path = tmp_path / target.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("existing adapter", encoding="utf-8")
    assert target.path in build_inventory(tmp_path)["unexpected_preexisting_components"]


def test_inventory_rejects_symlinked_source_anchor(tmp_path: Path) -> None:
    seed(tmp_path)
    target = tmp_path / ANCHORS[0].path
    target.unlink()
    try:
        target.symlink_to(tmp_path / ANCHORS[1].path)
    except (OSError, NotImplementedError):
        pytest.skip("No symlink support in this runner")
    with pytest.raises(ValueError, match="symlink"):
        build_inventory(tmp_path)
