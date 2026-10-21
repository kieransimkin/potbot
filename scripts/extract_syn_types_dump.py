#!/usr/bin/env python3
"""Statically extract SYN type declarations from a PostgreSQL plain-text dump.

This script never executes SQL, imports SYN, connects to a database or restores a
dump. It interprets only COPY text rows for types, typeParents and typeProperties
in the selected schema. All other COPY bodies are skipped without decoding their
fields. The whole input is hashed for provenance; instance, credential, job and
configuration contents are not copied into either output.

Run with the sibling export_syn_snapshot.py available; both scripts use only the
Python standard library on this path::

    python scripts/extract_syn_types_dump.py --dump /path/to/syn.sql \
        --output vocab/syn-database-types.snapshot.json \
        --provenance vocab/syn-database-types.provenance.json \
        --source-label syn-database-types --expected-sha256 THE_DUMP_SHA256

The snapshot contains the complete selected native type registry and objects=[].
Public type metadata preserves decoded COPY values and JSON numeric spellings.
Original pre-JSONB whitespace or number spellings are not recoverable. Neither
output may overwrite the source dump. Output files are individually atomic.
"""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path
from typing import Any

from export_syn_snapshot import (
    MAX_ANCESTRY_PAIRS,
    MAX_SNAPSHOT_BYTES,
    MAX_TYPES,
    ExportError,
    _canonical,
    _public_json,
    _ref,
    _text,
    _validate_graph,
    snapshot_bytes,
    write_snapshot,
)

MAX_DUMP_BYTES = 1024 * 1024 * 1024
MAX_COPY_ROW_BYTES = 8 * 1024 * 1024
_IDENTIFIER = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_]*)'
_HEADER = re.compile(
    rf"COPY (?P<schema>{_IDENTIFIER})\.(?P<table>{_IDENTIFIER}) "
    r"\((?P<columns>.*)\) FROM stdin;"
)
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", re.ASCII)
_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)", re.ASCII)
_MAX_BIGINT = 2**63 - 1
_COLUMNS = {
    "types": (
        "id",
        "name",
        "wikipediaRef",
        "locality",
        "stats",
        "statsUpdated",
        "knowledge",
        "knowledgeUpdated",
        "confidence",
        "confidenceUpdated",
    ),
    "typeParents": ("childRef", "parentRef"),
    "typeProperties": ("type", "name", "propertyTypeRef", "min", "max"),
}
_ESCAPES = {ord("b"): 8, ord("f"): 12, ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("v"): 11}


def decode_copy_field(value: bytes) -> str | None:
    """Decode PostgreSQL COPY text escapes as bytes, then decode valid UTF-8."""
    if value == b"\\N":
        return None
    output = bytearray()
    index = 0
    while index < len(value):
        char = value[index]
        index += 1
        if char != 92:
            output.append(char)
            continue
        if index == len(value):
            raise ExportError("selected COPY field ends in an incomplete escape")
        char = value[index]
        index += 1
        if 48 <= char <= 55:
            digits = bytes([char])
            while index < len(value) and len(digits) < 3 and 48 <= value[index] <= 55:
                digits += bytes([value[index]])
                index += 1
            decoded = int(digits, 8)
            if decoded > 255:
                raise ExportError("selected COPY field has an out-of-byte-range octal escape")
            output.append(decoded)
        elif (
            char == ord("x")
            and index < len(value)
            and chr(value[index]) in "0123456789abcdefABCDEF"
        ):
            digits = value[index : index + 1]
            index += 1
            if index < len(value) and chr(value[index]) in "0123456789abcdefABCDEF":
                digits += value[index : index + 1]
                index += 1
            output.append(int(digits, 16))
        else:
            output.append(_ESCAPES.get(char, char))
    try:
        return output.decode("utf-8")
    except UnicodeError as error:
        raise ExportError("selected COPY field is not valid UTF-8") from error


def _unquote(identifier: str) -> str:
    return identifier[1:-1].replace('""', '"') if identifier.startswith('"') else identifier


def _public_metadata(row: dict[str, str | None]) -> str:
    values: list[str] = []
    for column in _COLUMNS["types"]:
        value = row[column]
        if value is None:
            encoded = "null"
        elif column in {"stats", "knowledge"}:
            encoded = value  # Retain persisted JSON numeric lexemes; never use a float.
        elif column in {"locality", "confidence"}:
            pattern = _INTEGER if column == "locality" else _NUMBER
            if not pattern.fullmatch(value):
                raise ExportError("public type metadata contains an invalid numeric value")
            encoded = value
        else:
            encoded = _canonical(value)
        values.append(_canonical(column) + ":" + encoded)
    metadata = "{" + ",".join(values) + "}"
    _public_json(metadata)
    return metadata


def extract_types_dump(
    dump: Path,
    *,
    source_label: str,
    schema: str = "public",
    expected_sha256: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse only the three selected COPY tables, returning checked public data."""
    _text(source_label, "source label", 2000)
    if expected_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ExportError("expected SHA-256 must be 64 lowercase hexadecimal characters")
    if dump.stat().st_size > MAX_DUMP_BYTES:
        raise ExportError("dump exceeds the static extractor's 1 GiB input limit")
    selected_rows: dict[str, list[dict[str, str | None]]] = {table: [] for table in _COLUMNS}
    sections: dict[str, dict[str, Any]] = {}
    versions: dict[str, str] = {}
    selected: str | None = None
    in_copy = False
    selected_bytes = 0
    digest = hashlib.sha256()
    byte_count = 0
    line_number = 0

    def hashed_read(stream: Any) -> bytes:
        nonlocal byte_count
        value = stream.readline(MAX_COPY_ROW_BYTES + 1)
        digest.update(value)
        byte_count += len(value)
        if byte_count > MAX_DUMP_BYTES:
            raise ExportError("dump exceeds the static extractor's 1 GiB input limit")
        return value

    with dump.open("rb") as stream:
        while raw := hashed_read(stream):
            line_number += 1
            if len(raw) > MAX_COPY_ROW_BYTES and not raw.endswith(b"\n"):
                if selected is not None:
                    raise ExportError("selected COPY row exceeds the bounded row size")
                while raw and not raw.endswith(b"\n"):
                    raw = hashed_read(stream)
                continue  # Large unrelated data is neither retained nor decoded.
            body = raw.removesuffix(b"\n").removesuffix(b"\r")
            if in_copy:
                if body == b"\\.":
                    in_copy, selected = False, None
                elif selected is not None:
                    cells = body.split(b"\t")
                    if len(cells) != len(_COLUMNS[selected]):
                        raise ExportError("selected COPY row does not match its declared columns")
                    selected_bytes += len(body)
                    if selected_bytes > MAX_SNAPSHOT_BYTES:
                        raise ExportError("selected type data exceeds the 64 MiB snapshot bound")
                    row = {
                        name: decode_copy_field(cell)
                        for name, cell in zip(_COLUMNS[selected], cells, strict=True)
                    }
                    selected_rows[selected].append(row)
                    limit = MAX_TYPES if selected == "types" else MAX_ANCESTRY_PAIRS
                    if len(selected_rows[selected]) > limit:
                        raise ExportError("selected COPY table exceeds its record count bound")
                continue
            if body.startswith(b"COPY ") and body.endswith(b" FROM stdin;"):
                in_copy = True
                try:
                    header = body.decode("ascii")
                except UnicodeError:
                    continue  # An unrelated table name may use non-ASCII identifiers.
                match = _HEADER.fullmatch(header)
                if match is None:
                    continue
                table = _unquote(match.group("table"))
                if _unquote(match.group("schema")) != schema or table not in _COLUMNS:
                    continue
                if table in sections:
                    raise ExportError("dump repeats a selected COPY section")
                columns = tuple(
                    _unquote(column.strip()) for column in match.group("columns").split(",")
                )
                if columns != _COLUMNS[table]:
                    raise ExportError(
                        "selected COPY columns do not match the current SYN type schema"
                    )
                selected = table
                sections[table] = {"header_line": line_number, "columns": list(columns)}
            else:
                for prefix, key in (
                    (b"-- Dumped from database version ", "database_version"),
                    (b"-- Dumped by pg_dump version ", "pg_dump_version"),
                    (b"-- Started on ", "dump_started_at"),
                ):
                    if body.startswith(prefix):
                        versions[key] = body[len(prefix) :].decode("ascii")
    if in_copy:
        raise ExportError("dump ends inside an unterminated COPY section")
    if set(sections) != set(_COLUMNS):
        raise ExportError("dump is missing one or more required SYN type COPY sections")
    source_hash = digest.hexdigest()
    if expected_sha256 is not None and source_hash != expected_sha256:
        raise ExportError("source dump SHA-256 does not match the expected digest")

    types: dict[str, dict[str, Any]] = {}
    for row in selected_rows["types"]:
        ref = _ref(row["id"], "type")
        if ref in types:
            raise ExportError("source contains duplicate type references")
        name = _text(row["name"], "type name")
        metadata_json = _public_metadata(row)
        metadata = _public_json(metadata_json)
        knowledge = metadata.get("knowledge")
        definition = knowledge.get("definition") if isinstance(knowledge, dict) else None
        types[ref] = {
            "ref": ref,
            "name": name,
            "parents": [],
            "definition": definition if isinstance(definition, str) else None,
            "properties": [],
            "metadata_json": metadata_json,
        }
    edges: set[tuple[str, str]] = set()
    for row in selected_rows["typeParents"]:
        child, parent = _ref(row["childRef"], "type"), _ref(row["parentRef"], "type")
        if child not in types or parent not in types:
            raise ExportError("source parent edge refers to a missing native type")
        if (child, parent) in edges:
            raise ExportError("source contains a duplicate type parent edge")
        edges.add((child, parent))
        types[child]["parents"].append(parent)
    properties: set[tuple[str, str]] = set()
    for row in selected_rows["typeProperties"]:
        owner, value_type = _ref(row["type"], "type"), _ref(row["propertyTypeRef"], "type")
        name = _text(row["name"], "property name", 1024)
        bounds = [row["min"], row["max"]]
        if any(value is None or not _INTEGER.fullmatch(value) for value in bounds):
            raise ExportError("source property cardinalities must be bigint integers")
        minimum, maximum = (int(value) for value in bounds)
        if not 0 <= minimum <= maximum <= _MAX_BIGINT:
            raise ExportError("source property cardinalities are out of range")
        if owner not in types or value_type not in types:
            raise ExportError("source property refers to a missing owner or range type")
        if (owner, name) in properties:
            raise ExportError("source contains duplicate property names for a type")
        properties.add((owner, name))
        types[owner]["properties"].append(
            {"name": name, "type_ref": value_type, "minimum": minimum, "maximum": maximum}
        )
    for record in types.values():
        record["parents"].sort()
        record["properties"].sort(key=lambda prop: prop["name"])
    _validate_graph(types)
    snapshot = {
        "schema_version": 1,
        "source_label": source_label,
        "catalog_mode": "complete",
        "types": [types[ref] for ref in sorted(types)],
        "objects": [],
    }
    encoded = snapshot_bytes(snapshot)
    for table, section in sections.items():
        section["row_count"] = len(selected_rows[table])
    provenance = {
        "schema_version": 1,
        "id": "urn:potbot:syn-database-type-extraction:1",
        "method": "postgresql-copy-text-types-v1",
        "sql_executed": False,
        "source_basename": dump.name,
        "source_sha256": source_hash,
        "source_bytes": byte_count,
        "source_schema": schema,
        "source_header": versions,
        "selected_sections": sections,
        "type_count": len(types),
        "direct_parent_edges": len(edges),
        "direct_property_declarations": len(properties),
        "object_count": 0,
        "type_refs": sorted(types),
        "snapshot_sha256": hashlib.sha256(encoded).hexdigest(),
        "snapshot_bytes": len(encoded),
        "coverage": "complete native type registry in the selected dump COPY sections; no instances",
        "omitted": [
            "all instance records",
            "credentials",
            "non-type relationships",
            "chronicles",
            "jobs",
            "configuration",
        ],
    }
    return snapshot, provenance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dump", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--source-label", default="syn-database-types")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--expected-sha256")
    args = parser.parse_args(argv)
    try:
        resolved = [path.resolve() for path in (args.dump, args.output, args.provenance)]
        if len(set(resolved)) != 3:
            raise ExportError("source dump, snapshot and provenance paths must be distinct")
        for destination in (args.output, args.provenance):
            if destination.exists() and destination.samefile(args.dump):
                raise ExportError("outputs must not overwrite the source dump")
        snapshot, provenance = extract_types_dump(
            args.dump,
            source_label=args.source_label,
            schema=args.schema,
            expected_sha256=args.expected_sha256,
        )
        write_snapshot(args.output, snapshot)
        write_snapshot(args.provenance, provenance)
    except ExportError as error:
        parser.exit(2, f"Type extraction failed: {error}\n")
    except (OSError, UnicodeError):
        parser.exit(2, "Type extraction failed: input or output file is unavailable or invalid.\n")
    print(
        _canonical(
            {
                key: provenance[key]
                for key in (
                    "type_count",
                    "direct_parent_edges",
                    "direct_property_declarations",
                    "object_count",
                    "source_sha256",
                    "snapshot_sha256",
                    "sql_executed",
                )
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
