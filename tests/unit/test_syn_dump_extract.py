"""Static dump extraction preserves public types and never interprets other data."""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from potbot.domain.syn import SynSnapshot
from potbot.semantics.catalog import merge_types

ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXPORT = load_script("export_syn_snapshot")
_previous = sys.modules.get("export_syn_snapshot")
sys.modules["export_syn_snapshot"] = EXPORT
try:
    EXTRACT = load_script("extract_syn_types_dump")
finally:
    if _previous is None:
        sys.modules.pop("export_syn_snapshot", None)
    else:
        sys.modules["export_syn_snapshot"] = _previous


def copy_cell(value: str | None) -> bytes:
    if value is None:
        return b"\\N"
    return (
        value.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .encode()
    )


def type_row(
    ref: str,
    *,
    name: str = "A type",
    knowledge: str = '{"definition":"A public definition","precise":1.2300}',
) -> tuple[str | None, ...]:
    return (
        ref,
        name,
        None,
        "0",
        '{"large":900719925474099312345,"tiny":1e-100}',
        "2026-10-21 00:00:00+00",
        knowledge,
        None,
        "0.12345678901234567890123456789",
        None,
    )


def section(table: str, rows: list[tuple[str | None, ...]]) -> bytes:
    columns = ", ".join(f'"{column}"' for column in EXTRACT._COLUMNS[table])
    header = f'COPY public."{table}" ({columns}) FROM stdin;\n'.encode()
    return header + b"".join(b"\t".join(map(copy_cell, row)) + b"\n" for row in rows) + b"\\.\n"


def dump_bytes(
    *,
    types: list[tuple[str | None, ...]] | None = None,
    parents: list[tuple[str | None, ...]] | None = None,
    properties: list[tuple[str | None, ...]] | None = None,
) -> bytes:
    return (
        b"-- Dumped from database version 16.15\n-- Dumped by pg_dump version 16.15\n"
        + section(
            "typeParents",
            parents if parents is not None else [("syn://type/child", "syn://type/root")],
        )
        + section(
            "typeProperties",
            properties
            if properties is not None
            else [("syn://type/child", "related", "syn://type/root", "0", "3")],
        )
        + section(
            "types",
            types
            if types is not None
            else [type_row("syn://type/root", name="Racine café"), type_row("syn://type/child")],
        )
    )


def test_static_extraction_roundtrips_complete_public_registry_and_exact_numbers(
    tmp_path: Path,
) -> None:
    dump = tmp_path / "source.sql"
    # Even a COPY-looking row and invalid UTF-8 inside an unrelated body are ignored.
    unrelated = (
        b'COPY public.credentials ("privateData") FROM stdin;\n'
        b'COPY public.types ("wrong") FROM stdin;\n'
        b"privateData secret-that-must-not-leak \xff\n\\.\n"
    )
    raw = unrelated + dump_bytes()
    dump.write_bytes(raw)
    snapshot, provenance = EXTRACT.extract_types_dump(
        dump, source_label="fixture", expected_sha256=hashlib.sha256(raw).hexdigest()
    )
    checked = SynSnapshot.from_json(EXPORT.snapshot_bytes(snapshot).decode())
    assert checked.catalog_mode == "complete"
    assert checked.objects == ()
    assert len(checked.types) == 2
    assert len(merge_types(checked)) == 49
    by_ref = {record.ref: record for record in checked.types}
    assert by_ref["syn://type/root"].name == "Racine café"
    child = by_ref["syn://type/child"]
    assert child.parents == ("syn://type/root",)
    assert child.properties[0].minimum == 0
    assert child.properties[0].maximum == 3
    assert child.definition == "A public definition"
    for lexeme in ("1.2300", "1e-100", "900719925474099312345", "0.12345678901234567890123456789"):
        assert lexeme in child.metadata_json
    assert json.loads(child.metadata_json)["wikipediaRef"] is None
    assert provenance["sql_executed"] is False
    assert provenance["source_bytes"] == len(raw)
    assert provenance["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert (
        provenance["snapshot_sha256"] == hashlib.sha256(EXPORT.snapshot_bytes(snapshot)).hexdigest()
    )
    assert provenance["direct_parent_edges"] == 1
    assert provenance["direct_property_declarations"] == 1
    assert provenance["type_refs"] == ["syn://type/child", "syn://type/root"]
    assert b"secret-that-must-not-leak" not in EXPORT.snapshot_bytes(snapshot)
    assert "secret-that-must-not-leak" not in json.dumps(provenance)
    assert dump.read_bytes() == raw


@pytest.mark.parametrize(
    ("encoded", "decoded"),
    [
        (b"\\N", None),
        (b"\\\\N", "\\N"),
        (b"caf\\303\\251", "café"),
        (b"\\x63\\x61f\\xc3\\xa9", "café"),
        (b"a\\tb\\nc\\r\\\\d", "a\tb\nc\r\\d"),
        (b"\\q", "q"),
    ],
)
def test_copy_escapes_decode_before_utf8(encoded: bytes, decoded: str | None) -> None:
    assert EXTRACT.decode_copy_field(encoded) == decoded


@pytest.mark.parametrize("encoded", [b"trailing\\", b"\\777", b"\\xff"])
def test_invalid_copy_escapes_are_rejected(encoded: bytes) -> None:
    with pytest.raises(EXTRACT.ExportError):
        EXTRACT.decode_copy_field(encoded)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (section("types", []), "missing"),
        (dump_bytes() + section("types", []), "repeats"),
        (dump_bytes().removesuffix(b"\\.\n"), "unterminated"),
        (dump_bytes().replace(b'("childRef", "parentRef")', b'("wrong", "parentRef")'), "columns"),
        (dump_bytes(types=[type_row("syn://type/root"), type_row("syn://type/root")]), "duplicate"),
        (dump_bytes(parents=[("syn://type/child", "syn://type/missing")]), "missing"),
        (
            dump_bytes(
                parents=[
                    ("syn://type/child", "syn://type/root"),
                    ("syn://type/root", "syn://type/child"),
                ]
            ),
            "cycle",
        ),
        (
            dump_bytes(properties=[("syn://type/child", "bad", "syn://type/root", "3", "1")]),
            "range",
        ),
        (
            dump_bytes(properties=[("syn://type/child", "bad", "syn://type/missing", "0", "1")]),
            "missing",
        ),
        (
            dump_bytes(
                types=[type_row("syn://type/root", knowledge='{"nested":{"privateData":"secret"}}')]
            ),
            "private credential field",
        ),
    ],
)
def test_incomplete_ambiguous_or_private_type_data_fails(
    tmp_path: Path, raw: bytes, message: str
) -> None:
    dump = tmp_path / "bad.sql"
    dump.write_bytes(raw)
    with pytest.raises(EXTRACT.ExportError, match=message):
        EXTRACT.extract_types_dump(dump, source_label="bad-fixture")


def test_cli_checks_digest_and_protects_source_before_any_output_replace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    dump = tmp_path / "source.sql"
    raw = dump_bytes()
    dump.write_bytes(raw)
    output = tmp_path / "snapshot.json"
    provenance = tmp_path / "provenance.json"
    output.write_bytes(b"previous complete snapshot")
    provenance.write_bytes(b"previous complete provenance")
    arguments = ["--dump", str(dump), "--output", str(output), "--provenance", str(provenance)]
    with pytest.raises(SystemExit) as failure:
        EXTRACT.main([*arguments, "--expected-sha256", "0" * 64])
    assert failure.value.code == 2
    assert "digest" in capsys.readouterr().err
    assert output.read_bytes() == b"previous complete snapshot"
    assert provenance.read_bytes() == b"previous complete provenance"
    with pytest.raises(SystemExit):
        EXTRACT.main(["--dump", str(dump), "--output", str(dump), "--provenance", str(provenance)])
    assert "distinct" in capsys.readouterr().err
    monkeypatch.setattr(EXTRACT, "MAX_SNAPSHOT_BYTES", 50)
    with pytest.raises(SystemExit):
        EXTRACT.main(arguments)
    assert output.read_bytes() == b"previous complete snapshot"
    assert provenance.read_bytes() == b"previous complete provenance"
    assert dump.read_bytes() == raw
    monkeypatch.setattr(EXTRACT, "MAX_SNAPSHOT_BYTES", 64 * 1024 * 1024)
    capsys.readouterr()
    assert EXTRACT.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["sql_executed"] is False
    assert SynSnapshot.from_json(output.read_text()).objects == ()
    report = json.loads(provenance.read_text())
    assert report["snapshot_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert len(list(tmp_path.iterdir())) == 3


def test_selected_rows_are_bounded_while_large_unrelated_rows_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dump = tmp_path / "source.sql"
    monkeypatch.setattr(EXTRACT, "MAX_COPY_ROW_BYTES", 512)
    dump.write_bytes(
        b"COPY public.unrelated (value) FROM stdin;\n" + b"x" * 2000 + b"\n\\.\n" + dump_bytes()
    )
    assert len(EXTRACT.extract_types_dump(dump, source_label="fixture")[0]["types"]) == 2
    dump.write_bytes(dump_bytes(types=[type_row("syn://type/root", name="x" * 2000)]))
    with pytest.raises(EXTRACT.ExportError, match="row size"):
        EXTRACT.extract_types_dump(dump, source_label="fixture")
