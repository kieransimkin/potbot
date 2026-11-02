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
    current = audit.build_inventory(audit.ROOT, policy)
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
