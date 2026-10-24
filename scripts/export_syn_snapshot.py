#!/usr/bin/env python3
"""Export a public SYN typing snapshot using an existing SYN Python environment.

Example, from the installed SYN project environment::

    uv run --no-sync python /path/to/export_syn_snapshot.py \
        --config /path/to/syn.toml --source-label local-syn --output snapshot.json

Alternatively select an existing environment variable with --database-url-env.
Use --types-only to export the complete type registry without reading any instance
table, for example when the full graph exceeds the bounded snapshot size. This
explicit mode writes objects=[]; it never claims to include the source's objects.
Only SQLAlchemy and the PostgreSQL driver already used by SYN are required. This
script imports neither SYN nor Potbot, invokes no migrations, and never starts an
application or reads credential seeds. The existing current SYN schema is required.

Coverage: all type definitions, parent edges, direct property declarations, and the
nine instance tables visible to the configured database role. Only explicitly
listed public columns are exported. Credential privateData is never selected;
privateData/private_data keys inside public JSON cause an explicit failure.
Non-type relationships, event-target links, chronicles and job tables are omitted.

JSONB is serialized to text inside PostgreSQL, retaining its persisted numeric
representation without a floating-point conversion. Original whitespace or number
spellings discarded when JSONB was first stored cannot be recovered. Public binary
values are tagged base64 objects. No records are silently truncated. The completed
bounded file atomically replaces --output only after a successful read-only,
repeatable-read transaction. Connection URLs and database error details are never
included in exported data or diagnostics.
"""

from __future__ import annotations

import argparse
import heapq
import json
import os
import re
import tomllib
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

MAX_TYPES = 10_000
MAX_OBJECTS = 100_000
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_PUBLIC_JSON_BYTES = 2_000_000
MAX_JSON_DEPTH = 128
MAX_ANCESTRY_PAIRS = 1_000_000
_MAX_BIGINT = 2**63 - 1
_KINDS = ("thing", "event", "ember", "cog", "credential", "enigma", "pondle", "snug", "om")
_TABLES = {
    "thing": "things",
    "event": "events",
    "ember": "embers",
    "cog": "cogs",
    "credential": "credentials",
    "enigma": "enigmas",
    "pondle": "pondles",
    "snug": "snugs",
    "om": "oms",
}
_REF = re.compile(
    r"syn://(?P<kind>type|thing|event|ember|cog|credential|enigma|pondle|snug|om)/"
    r"(?P<slug>[a-z0-9]+(?:[-_][a-z0-9]+)*)",
    re.ASCII,
)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}", re.ASCII)
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
_SOURCE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,199}", re.ASCII)
_PUBLIC_COLUMNS = (
    "name",
    "locality",
    "stats",
    "statsUpdated",
    "knowledge",
    "knowledgeUpdated",
    "confidence",
    "confidenceUpdated",
)


class ExportError(ValueError):
    """A safe, bounded export failure with no connection details in its message."""


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _unique_public(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExportError("public JSON contains duplicate keys")
        if key in {"privateData", "private_data"}:
            raise ExportError("public JSON contains a private credential field")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ExportError("public JSON contains a non-finite constant")


def _public_json(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        raise ExportError("a public JSON record exceeds its size limit or is not text")
    try:
        if len(value.encode("utf-8")) > MAX_PUBLIC_JSON_BYTES:
            raise ExportError("a public JSON record exceeds its size limit")
        depth, quoted, escaped = 0, False, False
        for char in value:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    raise ExportError("public JSON exceeds its 128-level nesting limit")
            elif char in "]}":
                depth -= 1
        parsed = json.loads(
            value,
            parse_int=Decimal,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_public,
        )
    except (UnicodeError, json.JSONDecodeError, InvalidOperation, RecursionError) as error:
        raise ExportError("invalid or excessively nested public JSON") from error
    if not isinstance(parsed, dict):
        raise ExportError("public JSON must contain an object")
    return parsed


def _ref(value: object, kind: str | None = None) -> str:
    if not isinstance(value, str):
        raise ExportError("SYN references must be text")
    match = _REF.fullmatch(value)
    if match is None or len(match.group("slug")) > 255:
        raise ExportError("source contains an invalid local SYN reference")
    if kind is not None and match.group("kind") != kind:
        raise ExportError("source reference has an unexpected SYN kind")
    return value


def _text(value: object, field: str, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExportError(f"source {field} must be a nonempty string")
    if maximum is not None and len(value) > maximum:
        raise ExportError(f"source {field} exceeds its length limit")
    return value


def _qualified(schema: str, table: str) -> str:
    if not _IDENTIFIER.fullmatch(schema):
        raise ExportError("schema must be a simple PostgreSQL identifier of at most 63 characters")
    # Both identifiers are checked or fixed constants; none is source SQL.
    return f'"{schema}"."{table}"'


def _public_projection(kind: str) -> str:
    columns = ("id", *_PUBLIC_COLUMNS)
    if kind != "type":
        columns = ("id", "typeRef", *_PUBLIC_COLUMNS)
    if kind in {"type", "thing"}:
        columns += ("wikipediaRef",)
    if kind == "event":
        columns += ("occurredAt",)
    pairs = [f"'{column}', t.\"{column}\"" for column in columns]
    if kind == "credential":
        pairs.append(
            "'publicKeyHash', CASE WHEN t.\"publicKeyHash\" IS NULL THEN NULL "
            "ELSE jsonb_build_object('$binary', jsonb_build_object("
            "'encoding', 'base64', 'data', replace(encode(t.\"publicKeyHash\", 'base64'), "
            "chr(10), ''))) END"
        )
    return "jsonb_build_object(" + ", ".join(pairs) + ")::text"


def public_record_query(schema: str, kind: str) -> str:
    """Build an allowlisted public projection; never reflect or select entire rows."""
    if kind != "type" and kind not in _TABLES:
        raise ExportError("unsupported SYN kind")
    table = _qualified(schema, "types" if kind == "type" else _TABLES[kind])
    extra = 't."name" AS name' if kind == "type" else 't."typeRef"::text AS type_ref'
    output = "metadata_json" if kind == "type" else "payload_json"
    bound = MAX_TYPES if kind == "type" else MAX_OBJECTS
    # Oversized rows return NULL instead of transferring an unbounded text value.
    return (
        f'SELECT t."id"::text AS ref, {extra}, '
        f"CASE WHEN octet_length(p.public_json) <= {MAX_PUBLIC_JSON_BYTES} "
        f"THEN p.public_json ELSE NULL END AS {output} "
        f"FROM {table} AS t CROSS JOIN LATERAL "
        f"(SELECT {_public_projection(kind)} AS public_json) AS p "
        f'ORDER BY t."id"::text COLLATE "C" LIMIT {bound + 1}'
    )


def _rows(connection: Any, statement: str) -> Any:
    return connection.exec_driver_sql(
        statement, execution_options={"stream_results": True, "max_row_buffer": 100}
    ).mappings()


def _validate_graph(types: Mapping[str, dict[str, Any]]) -> None:
    counts: dict[str, int] = {}
    children: dict[str, list[str]] = {ref: [] for ref in types}
    for ref, record in types.items():
        counts[ref] = len(record["parents"])
        for parent in record["parents"]:
            if parent not in types:
                raise ExportError("source type catalog contains a missing parent")
            children[parent].append(ref)
        for prop in record["properties"]:
            if prop["type_ref"] not in types:
                raise ExportError("source type property refers to a missing range type")
    ready = [ref for ref, count in counts.items() if not count]
    heapq.heapify(ready)
    ancestors: dict[str, set[str]] = {}
    total = 0
    while ready:
        ref = heapq.heappop(ready)
        inherited = {ref}
        for parent in types[ref]["parents"]:
            inherited.update(ancestors[parent])
            if total + len(inherited) > MAX_ANCESTRY_PAIRS:
                raise ExportError("source type ancestry exceeds its unique-pair limit")
        total += len(inherited)
        if total > MAX_ANCESTRY_PAIRS:
            raise ExportError("source type ancestry exceeds its unique-pair limit")
        ancestors[ref] = inherited
        for child in children[ref]:
            counts[child] -= 1
            if not counts[child]:
                heapq.heappush(ready, child)
    if len(ancestors) != len(types):
        raise ExportError("source type catalog contains a cycle")


def collect_snapshot(
    connection: Any, *, source_label: str, schema: str = "public", types_only: bool = False
) -> dict[str, Any]:
    """Read using a SQLAlchemy Core connection already fenced to a read-only snapshot."""
    if not isinstance(source_label, str) or not _SOURCE_LABEL.fullmatch(source_label):
        raise ExportError(
            "source label must be 1..200 nonsecret letters, digits, spaces, dots or dashes"
        )
    if type(types_only) is not bool:
        raise ExportError("types_only must be boolean")
    _qualified(schema, "types")
    total_bytes = 0

    def charge(record: object) -> None:
        nonlocal total_bytes
        total_bytes += len(_canonical(record).encode("utf-8")) + 1
        if total_bytes >= MAX_SNAPSHOT_BYTES:
            raise ExportError("snapshot exceeds its 64 MiB byte limit")

    types: dict[str, dict[str, Any]] = {}
    for row in _rows(connection, public_record_query(schema, "type")):
        if len(types) >= MAX_TYPES:
            raise ExportError("snapshot exceeds its type count limit")
        ref = _ref(row["ref"], "type")
        if ref in types:
            raise ExportError("source contains duplicate type references")
        metadata = _public_json(row["metadata_json"])
        name = _text(row["name"], "type name")
        if metadata.get("id") != ref or metadata.get("name") != name:
            raise ExportError("source type projection has inconsistent identity")
        knowledge = metadata.get("knowledge")
        definition = knowledge.get("definition") if isinstance(knowledge, dict) else None
        if definition is not None and not isinstance(definition, str):
            definition = None  # The original public metadata remains preserved losslessly.
        record = {
            "ref": ref,
            "name": name,
            "parents": [],
            "definition": definition,
            "properties": [],
            "metadata_json": row["metadata_json"],
        }
        charge(record)
        types[ref] = record

    parent_query = (
        'SELECT "childRef"::text AS child_ref, "parentRef"::text AS parent_ref '
        f"FROM {_qualified(schema, 'typeParents')} "
        'ORDER BY "childRef"::text COLLATE "C", "parentRef"::text COLLATE "C"'
    )
    edges: set[tuple[str, str]] = set()
    for row in _rows(connection, parent_query):
        child, parent = _ref(row["child_ref"], "type"), _ref(row["parent_ref"], "type")
        if child not in types or parent not in types:
            raise ExportError("source parent edge has an unknown type")
        if (child, parent) in edges:
            raise ExportError("source contains a duplicate type parent edge")
        edges.add((child, parent))
        charge(parent)
        types[child]["parents"].append(parent)

    property_query = (
        'SELECT "type"::text AS owner_ref, "name", "propertyTypeRef"::text AS type_ref, '
        '"min" AS minimum, "max" AS maximum '
        f"FROM {_qualified(schema, 'typeProperties')} "
        'ORDER BY "type"::text COLLATE "C", "name" COLLATE "C"'
    )
    properties: set[tuple[str, str]] = set()
    for row in _rows(connection, property_query):
        owner, value_type = _ref(row["owner_ref"], "type"), _ref(row["type_ref"], "type")
        name = _text(row["name"], "property name", 1024)
        minimum, maximum = row["minimum"], row["maximum"]
        if (
            type(minimum) is not int
            or type(maximum) is not int
            or not 0 <= minimum <= maximum <= _MAX_BIGINT
        ):
            raise ExportError(
                "source property cardinality must have finite nonnegative bigint bounds"
            )
        if owner not in types or value_type not in types:
            raise ExportError("source property has an unknown owner or range type")
        if (owner, name) in properties:
            raise ExportError("source contains duplicate property names for a type")
        properties.add((owner, name))
        prop = {"name": name, "type_ref": value_type, "minimum": minimum, "maximum": maximum}
        charge(prop)
        types[owner]["properties"].append(prop)
    _validate_graph(types)

    objects: list[dict[str, str]] = []
    refs: set[str] = set()
    for kind in () if types_only else _KINDS:
        for row in _rows(connection, public_record_query(schema, kind)):
            if len(objects) >= MAX_OBJECTS:
                raise ExportError("snapshot exceeds its object count limit")
            ref, type_ref = _ref(row["ref"], kind), _ref(row["type_ref"], "type")
            if ref in refs:
                raise ExportError("source contains duplicate object references")
            if type_ref not in types:
                raise ExportError("source object refers to a missing native type")
            payload = _public_json(row["payload_json"])
            if payload.get("id") != ref or payload.get("typeRef") != type_ref:
                raise ExportError("source object projection has inconsistent identity")
            _text(payload.get("name"), "object name")
            record = {"ref": ref, "type_ref": type_ref, "payload_json": row["payload_json"]}
            charge(record)
            objects.append(record)
            refs.add(ref)
    snapshot = {
        "schema_version": 1,
        "source_label": source_label,
        "catalog_mode": "complete",
        "types": [types[ref] for ref in sorted(types)],
        "objects": sorted(objects, key=lambda record: record["ref"]),
    }
    snapshot_bytes(
        snapshot
    )  # Include all envelope punctuation and UTF-8 escaping in the final limit.
    return snapshot


def snapshot_bytes(snapshot: dict[str, Any]) -> bytes:
    encoded = (_canonical(snapshot) + "\n").encode("utf-8")
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise ExportError("snapshot exceeds its 64 MiB byte limit")
    return encoded


def write_snapshot(output: Path, snapshot: dict[str, Any]) -> None:
    """Complete and fsync a private temporary file before atomically replacing output."""
    encoded = snapshot_bytes(snapshot)
    # Load the standard-library-only helper by source path: SYN's existing
    # environment needs neither Potbot installed nor its package imports.
    helper = Path(__file__).resolve().parents[1] / "src/potbot/tools/temporary.py"
    spec = spec_from_file_location("potbot_export_temporary", helper)
    if spec is None or spec.loader is None:
        raise ExportError("the bundled temporary-storage helper is unavailable")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    descriptor, temporary = module.allocate_file(output.parent, prefix=".potbot-snapshot-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def _database_url(args: argparse.Namespace) -> str:
    if args.config is not None:
        try:
            with args.config.open("rb") as stream:
                config = tomllib.load(stream)
            value = config.get("database", {}).get("url")
        except (OSError, ValueError, AttributeError) as error:
            raise ExportError(
                "could not read database.url from the selected SYN TOML config"
            ) from error
    else:
        if not _ENVIRONMENT_NAME.fullmatch(args.database_url_env):
            raise ExportError("database environment variable name is invalid")
        value = os.environ.get(args.database_url_env)
    if not isinstance(value, str) or not value.strip():
        raise ExportError("the selected config or environment variable must define a database URL")
    if not value.startswith(("postgresql://", "postgresql+psycopg://", "postgresql+psycopg2://")):
        raise ExportError("the exporter requires a PostgreSQL SQLAlchemy URL")
    # SYN's normal driver is psycopg3; avoid an accidental psycopg2 dependency.
    if value.startswith("postgresql://"):
        value = "postgresql+psycopg://" + value[len("postgresql://") :]
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    config = parser.add_mutually_exclusive_group(required=True)
    config.add_argument(
        "--config", type=Path, help="existing SYN TOML file; reads database.url only"
    )
    config.add_argument("--database-url-env", help="name of an existing environment variable")
    parser.add_argument(
        "--source-label", required=True, help="short nonsecret label, never a URL or DSN"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--schema", default="public")
    parser.add_argument(
        "--types-only",
        action="store_true",
        help="export all type definitions, parents and direct properties; do not read instance tables",
    )
    args = parser.parse_args(argv)
    engine = None
    try:
        database_url = _database_url(args)
        # Deliberately no import of syn.database (which defaults to migrations).
        from sqlalchemy import create_engine

        engine = create_engine(database_url, echo=False, hide_parameters=True)
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            with connection.begin():
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                connection.exec_driver_sql("SET LOCAL TIME ZONE 'UTC'")
                connection.exec_driver_sql("SET LOCAL DateStyle = 'ISO, YMD'")
                snapshot = collect_snapshot(
                    connection,
                    source_label=args.source_label,
                    schema=args.schema,
                    types_only=args.types_only,
                )
        write_snapshot(args.output, snapshot)
    except ImportError:
        parser.exit(
            2,
            "Export failed: run this script in the existing SYN SQLAlchemy/psycopg environment.\n",
        )
    except ExportError as error:
        parser.exit(2, f"Export failed: {error}\n")
    except Exception as error:
        parser.exit(
            2,
            f"Export failed ({type(error).__name__}); check source read access, the complete current SYN "
            "schema and output permissions. Connection and query details are omitted.\n",
        )
    finally:
        if engine is not None:
            engine.dispose()
    print(
        _canonical(
            {
                "output": str(args.output),
                "source_label": args.source_label,
                "types": len(snapshot["types"]),
                "objects": len(snapshot["objects"]),
                "types_only": args.types_only,
                "coverage": (
                    "complete type registry, parents and direct type properties visible to the database role; no instance records"
                    if args.types_only
                    else "public primary records, type parents and direct type properties visible to the database role"
                ),
                "omitted": [
                    *(["all instance records"] if args.types_only else []),
                    "private credential data",
                    "non-type relationships",
                    "event targets",
                    "chronicles",
                    "jobs",
                ],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
