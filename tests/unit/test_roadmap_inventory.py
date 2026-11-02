"""U00-A must retain source coverage without manufacturing runtime acceptance."""

from __future__ import annotations

import copy
import json
import subprocess
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from scripts import roadmap_inventory as audit


def word(*paragraphs: str) -> bytes:
    data = BytesIO()
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
        + "</w:body></w:document>"
    )
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return data.getvalue()


def test_retained_binary_versions_preserve_all_added_steps_and_distinct_namespaces() -> None:
    policy = audit.load_json(audit.ROOT / audit.POLICY)
    revised = next(row for row in policy["programmes"] if row["namespace"] == "plan-1.1")
    old = next(row for row in policy["programmes"] if row["namespace"] == "plan-1.0")
    new_ids = {row["id"] for row in audit.source_stages(audit.ROOT, revised)}
    old_ids = {row["id"] for row in audit.source_stages(audit.ROOT, old)}
    assert old_ids <= new_ids
    assert new_ids - old_ids == {f"P{i:03}" for i in range(154, 205)}
    gates = next(row for row in policy["programmes"] if row["namespace"] == "architecture-1.1")
    assert {row["id"] for row in audit.source_stages(audit.ROOT, gates)} == {
        f"G{i:02}" for i in range(1, 36)
    }


def test_stage_locator_is_document_xml_paragraph_and_does_not_claim_a_page(tmp_path: Path) -> None:
    (tmp_path / "plan.docx").write_bytes(word("Step P001 Preserve evidence", "Status Completed"))
    programme = {
        "namespace": "synthetic",
        "source": "plan.docx",
        "format": "word_steps",
        "pattern": r"^Step (?P<id>P\d{3}) (?P<title>.+)$",
        "expected_ids": ["P001"],
    }
    (row,) = audit.source_stages(tmp_path, programme)
    assert row["locator"] == "word/document.xml:paragraph:1"
    assert row["source_claim"] == "completed"
    assert "qualified" not in row


@pytest.mark.parametrize(
    "paragraphs",
    [
        ("Step P001 One", "Step P001 Duplicate"),
        ("Step P001 One", "Step P002 Newly omitted step"),
        ("No stage present",),
    ],
)
def test_missing_new_or_duplicated_word_steps_cannot_silently_pass(
    tmp_path: Path, paragraphs
) -> None:
    (tmp_path / "plan.docx").write_bytes(word(*paragraphs))
    programme = {
        "namespace": "synthetic",
        "source": "plan.docx",
        "format": "word_steps",
        "pattern": r"^Step (?P<id>P\d{3}) (?P<title>.+)$",
        "expected_ids": ["P001"],
    }
    with pytest.raises(ValueError, match="Duplicate stage|Stage coverage drift"):
        audit.source_stages(tmp_path, programme)


@pytest.mark.parametrize(
    "change",
    [
        {"format": "unknown"},
        {"expected_ids": []},
        {"expected_ids": ["P001", "P001"]},
    ],
)
def test_unknown_parser_and_ambiguous_expected_ids_fail(tmp_path: Path, change) -> None:
    (tmp_path / "plan.docx").write_bytes(word("Step P001 Preserve source"))
    programme = {
        "namespace": "synthetic",
        "source": "plan.docx",
        "format": "word_steps",
        "pattern": r"^Step (?P<id>P\d{3}) (?P<title>.+)$",
        "expected_ids": ["P001"],
        **change,
    }
    with pytest.raises(
        ValueError, match="Unsupported source extraction|Invalid expected stage IDs"
    ):
        audit.source_stages(tmp_path, programme)


@pytest.mark.parametrize("raw", [b"not an archive", b"", b"PK broken"])
def test_broken_word_sources_fail_closed(raw: bytes) -> None:
    with pytest.raises(ValueError, match="Invalid Word"):
        audit.word_paragraphs(raw)


def test_word_parser_rejects_entities_and_oversized_xml(monkeypatch) -> None:
    data = BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("word/document.xml", '<!DOCTYPE x [<!ENTITY a "secret">]><x/>')
    with pytest.raises(ValueError, match="Unsafe XML"):
        audit.word_paragraphs(data.getvalue())
    monkeypatch.setattr(audit, "MAX_XML_BYTES", 10)
    with pytest.raises(ValueError, match="Oversized Word"):
        audit.word_paragraphs(word("bounded source"))


@pytest.mark.parametrize(
    "name", ["../private.md", "/outside.md", "C:/outside.md", "a\\b.md", "a/./b.md"]
)
def test_source_paths_cannot_escape_or_alias_repository(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="Unsafe source path"):
        audit.safe_source(tmp_path, name)


def test_new_unignored_document_is_discovered_without_staging(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / ".gitignore").write_text("private/\n", encoding="utf-8")
    (tmp_path / "docs/new-plan.md").write_text("New requirement", encoding="utf-8")
    (tmp_path / "private").mkdir()
    (tmp_path / "private/notes.md").write_text("private evidence", encoding="utf-8")
    assert audit.documentation_paths(tmp_path) == ["docs/new-plan.md"]


def test_missing_tracked_document_and_source_mutation_are_not_a_pass(tmp_path: Path) -> None:
    (tmp_path / "plan.md").write_text("Before", encoding="utf-8")
    before = audit.source_record(tmp_path, "plan.md")
    (tmp_path / "plan.md").write_text("After", encoding="utf-8")
    assert audit.source_record(tmp_path, "plan.md")["sha256"] != before["sha256"]
    (tmp_path / "plan.md").unlink()
    with pytest.raises(ValueError, match="Missing source"):
        audit.source_record(tmp_path, "plan.md")


def test_json_duplicate_fields_cannot_replace_audit_policy(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text('{"runtime_qualified":false,"runtime_qualified":true}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate JSON"):
        audit.load_json(path)


def test_snapshot_rejects_stale_data_missing_namespace_and_false_closure() -> None:
    policy = audit.load_json(audit.ROOT / audit.POLICY)
    current = audit.build_inventory(audit.ROOT, policy, apply_review=False)
    assert current["runtime_qualified"] is False
    assert current["u00_exit_met"] is False
    keys = {row["key"] for row in current["requirements"]}
    assert {"architecture-1.1:G01", "tool-gaps:G01", "worker-gates:G1"} <= keys
    assert {"original-user-requirements:U01", "unified:U01"} <= keys
    assert all(
        row["evidence_state"] == "not_qualified_by_inventory" for row in current["requirements"]
    )
    altered = copy.deepcopy(current)
    altered["runtime_qualified"] = True
    with pytest.raises(ValueError):
        audit.validate_snapshot(altered, current)
    altered = copy.deepcopy(current)
    altered["sources"].pop()
    with pytest.raises(ValueError, match="Stale roadmap inventory"):
        audit.validate_snapshot(altered, current)
    missing = copy.deepcopy(policy)
    missing["programmes"].pop()
    with pytest.raises(ValueError, match="programme namespace"):
        audit.build_inventory(audit.ROOT, missing)


def test_source_inspection_never_imports_or_runs_document_examples(tmp_path: Path) -> None:
    path = tmp_path / "untrusted.md"
    path.write_text("```python\nraise RuntimeError('must not execute')\n```", encoding="utf-8")
    row = audit.source_record(tmp_path, "untrusted.md")
    assert row["interpretation"] == "source_content_not_runtime_evidence"
    assert row["sha256"]


def test_public_snapshot_has_no_private_evidence_or_host_paths() -> None:
    snapshot = audit.load_json(audit.ROOT / audit.SNAPSHOT)
    rendered = json.dumps(snapshot)
    assert "C:\\\\Users" not in rendered
    assert "document-text" not in rendered
    assert "blog-inventory" not in rendered


@pytest.fixture(scope="module")
def review_inputs():
    policy = audit.load_json(audit.ROOT / audit.POLICY)
    inventory = audit.build_inventory(audit.ROOT, policy, apply_review=False)
    review = audit.load_json(audit.ROOT / audit.REVIEW)
    return inventory, review


def test_complete_review_closes_only_audit_and_keeps_planned_work_visible(review_inputs) -> None:
    inventory, review = copy.deepcopy(review_inputs)
    audit.validate_review(audit.ROOT, inventory, review)
    assert inventory["u00_exit_met"] is True
    assert inventory["runtime_qualified"] is False
    assert len(inventory["requirements"]) == 731
    by_key = {r["key"]: r for r in inventory["requirements"]}
    assert by_key["object-memory:MG01.1"]["evidence_state"] == "reviewed_planned"
    assert by_key["syn-release:SYN-R05"]["evidence_state"] == "reviewed_qualified_subset"
    assert by_key["syn-release:SYN-R06"]["evidence_state"] == "reviewed_planned"
    assert all(r["assessment"] and r["blockers"] and r["next_action"] for r in by_key.values())
    covered = {u["source"] for u in inventory["narrative_coverage"]}
    assert covered == {s["path"] for s in inventory["sources"] if s["kind"] != "media"}
    assert {"guest-logs", "tool-upgrade", "journal"} <= {
        f for u in inventory["narrative_coverage"] for f in u["review_families"]
    }
    assert audit.REVIEW not in {s["path"] for s in inventory["sources"]}


@pytest.mark.parametrize(
    ("area", "change", "message"),
    [
        ("requirements", "remove", "Unreviewed stage"),
        ("requirements", "duplicate", "Duplicate stage"),
        ("requirements", "hash", "Stale stage"),
        ("requirements", "empty", "Empty stage"),
        ("requirements", "family", "Unmapped stage"),
        ("sources", "remove", "Unreviewed documentation"),
        ("sources", "hash", "Stale source"),
        ("sources", "unit", "Missing or stale narrative"),
        ("families", "owner", "Unknown review owner"),
        ("families", "hash", "Stale code/test"),
        ("families", "symbol", "Missing code/test symbol"),
        ("families", "unsupported", "Unsupported implementation"),
    ],
)
def test_review_rejects_incomplete_or_stale_traceability(review_inputs, area, change, message):
    inventory, review = copy.deepcopy(review_inputs)
    rows = review[area]
    if change == "remove":
        rows.pop()
    elif change == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    elif change == "hash":
        row = rows[0]["code"][0] if area == "families" else rows[0]
        row["requirement_sha256" if area == "requirements" else "sha256"] = "0" * 64
    elif change == "empty":
        rows[0]["assessment"] = ""
    elif change == "family":
        rows[0]["families"] = ["invented_owner"]
    elif change == "unit":
        next(s for s in rows if s["narrative"])["narrative"].pop()
    elif change == "owner":
        rows[0]["owners"] = ["U99"]
    elif change == "symbol":
        rows[0]["tests"][0]["symbol"] = "test_invented"
    elif change == "unsupported":
        rows[0]["code"] = []
    with pytest.raises(ValueError, match=message):
        audit.validate_review(audit.ROOT, inventory, review)
    assert inventory["u00_exit_met"] is False


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("revision", "CI belongs to another"),
        ("pending", "Unsuccessful baseline CI"),
        ("failed", "Unsuccessful baseline CI"),
        ("missing_job", "Missing baseline CI job"),
        ("missing_workflow", "Unmapped acceptance workflow"),
        ("workflow_hash", "Stale acceptance workflow"),
        ("missing_pr", "Incomplete pull-request"),
        ("branch_head", "Unpinned open-PR"),
        ("branch_owner", "Unmapped branch documentation"),
        ("inaccessible", "Unrecorded inaccessible"),
        ("runtime", "cannot qualify runtime"),
        ("unfinished", "Incomplete audit"),
    ],
)
def test_review_cannot_borrow_ci_or_lose_external_and_inaccessible_scope(
    review_inputs, change, message
):
    inventory, review = copy.deepcopy(review_inputs)
    github = review["github"]
    checks = github["baseline_checks"]
    if change == "revision":
        checks["head_sha"] = "0" * 40
    elif change == "pending":
        checks["jobs"][0]["status"] = "queued"
    elif change == "failed":
        checks["jobs"][0]["conclusion"] = "failure"
    elif change == "missing_job":
        checks["jobs"] = [j for j in checks["jobs"] if j["name"] != "verify"]
    elif change == "missing_workflow":
        github["workflows"].pop()
    elif change == "workflow_hash":
        github["workflows"][0]["sha256"] = "0" * 64
    elif change == "missing_pr":
        github["pull_requests"].pop()
    elif change == "branch_head":
        github["open_pr_sources"][0]["head_sha"] = "0" * 40
    elif change == "branch_owner":
        github["open_pr_sources"][0]["sources"][0]["families"] = ["invented"]
    elif change == "inaccessible":
        review["inaccessible_sources"][0]["next_action"] = ""
    elif change == "runtime":
        review["runtime_qualified"] = True
    elif change == "unfinished":
        review["decision"] = "in_progress"
    with pytest.raises(ValueError, match=message):
        audit.validate_review(audit.ROOT, inventory, review)
    assert inventory["u00_exit_met"] is False


def test_narrative_coverage_includes_unlabelled_text_tables_and_examples(tmp_path: Path) -> None:
    path = tmp_path / "plan.md"
    path.write_text(
        "Future behavior without an ID or modal keyword.\n\n# Acceptance\n"
        "| case | needed |\n|---|---|\n| restart | retain state |\n"
        "```python\n# Example heading remains source data\nraise RuntimeError('never execute')\n```\n",
        encoding="utf-8",
    )
    units = audit.narrative_units(tmp_path, audit.source_record(tmp_path, "plan.md"))
    assert [u["locator"] for u in units] == ["line:1-2", "line:3-10"]
    (tmp_path / "plan.docx").write_bytes(word("Preface", "Requirement with no ID", ""))
    units = audit.narrative_units(tmp_path, audit.source_record(tmp_path, "plan.docx"))
    assert [u["locator"] for u in units] == ["paragraph:1-1", "paragraph:2-2"]


def test_planned_graph_work_cannot_be_promoted_without_implementation_evidence(review_inputs):
    inventory, review = copy.deepcopy(review_inputs)
    row = next(r for r in review["requirements"] if r["key"] == "object-memory:MG01.1")
    row["status"] = "qualified_subset"
    with pytest.raises(ValueError, match="Stage has no implementation evidence"):
        audit.validate_review(audit.ROOT, inventory, review)
