"""Standalone exporter boundaries without importing SYN or requiring SQLAlchemy."""

import importlib.util
import json
import re
import sys
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from potbot.domain.syn import SynSnapshot
from potbot.semantics.catalog import merge_types

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "export_syn_snapshot", ROOT / "scripts/export_syn_snapshot.py"
)
assert SPEC is not None and SPEC.loader is not None
EXPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPORT)


class Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def mappings(self) -> list[dict[str, Any]]:
        return self.rows


class Connection:
    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self.tables = tables
        self.queries: list[str] = []
        self.options: dict[str, Any] = {}

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def execution_options(self, **options: Any) -> "Connection":
        self.options.update(options)
        return self

    def begin(self) -> Any:
        return nullcontext()

    def exec_driver_sql(self, query: str, **kwargs: Any) -> Rows:
        self.queries.append(query)
        if query.startswith("SET "):
            return Rows([])
        match = re.search(r'FROM "[^"]+"\."([^"]+)"', query)
        assert match is not None
        assert kwargs["execution_options"]["stream_results"]
        return Rows(self.tables[match.group(1)])


@pytest.fixture
def source() -> Connection:
    types = [
        {
            "ref": "syn://type/thing",
            "name": "thing",
            "metadata_json": '{"id":"syn://type/thing","name":"thing","knowledge":{"ontologyVersion":4}}',
        },
        {
            "ref": "syn://type/person",
            "name": "person",
            "metadata_json": '{"id":"syn://type/person","name":"person","knowledge":{"definition":"A person","exact":0.12345678901234567890123456789}}',
        },
    ]
    tables: dict[str, list[dict[str, Any]]] = {
        "types": types,
        "typeParents": [{"child_ref": "syn://type/person", "parent_ref": "syn://type/thing"}],
        "typeProperties": [
            {
                "owner_ref": "syn://type/person",
                "name": "friend",
                "type_ref": "syn://type/person",
                "minimum": 0,
                "maximum": 3,
            }
        ],
    }
    for kind, table in EXPORT._TABLES.items():
        ref = f"syn://{kind}/example"
        payload = (
            '{"id":' + json.dumps(ref) + ',"typeRef":"syn://type/person","name":"example",'
            '"knowledge":{"large":9007199254740993,"small":1e-100,"decimal":1.2300}}'
        )
        if kind == "credential":
            payload = (
                payload[:-1] + ',"publicKeyHash":{"$binary":{"encoding":"base64","data":"AQID"}}}'
            )
        tables[table] = [{"ref": ref, "type_ref": "syn://type/person", "payload_json": payload}]
    return Connection(tables)


def test_complete_export_roundtrips_preserving_exact_public_json_and_all_nine_kinds(
    source: Connection,
) -> None:
    snapshot = EXPORT.collect_snapshot(source, source_label="unit-fixture")
    checked = SynSnapshot.from_json(EXPORT.snapshot_bytes(snapshot).decode())
    assert checked.catalog_mode == "complete"
    assert len(checked.types) == 2
    assert len(checked.objects) == 9
    assert {record.ref.kind for record in checked.objects} == set(EXPORT._KINDS)
    assert len(merge_types(checked)) == 49
    for record in checked.objects:
        assert (
            record.payload_json == source.tables[EXPORT._TABLES[record.ref.kind]][0]["payload_json"]
        )
        assert "9007199254740993" in record.payload_json
        assert "1e-100" in record.payload_json
        assert "1.2300" in record.payload_json
    person = next(record for record in checked.types if record.name == "person")
    assert person.definition == "A person"
    assert person.properties[0].minimum == 0
    assert person.properties[0].maximum == 3
    assert "0.12345678901234567890123456789" in person.metadata_json
    assert all(
        "privateData" not in query and "private_data" not in query for query in source.queries
    )
    assert all("SELECT *" not in query.upper() for query in source.queries)
    assert len(source.queries) == 12
    credential_query = next(query for query in source.queries if '"credentials"' in query)
    assert "'$binary'" in credential_query and "'base64'" in credential_query
    assert "::text" in credential_query


def test_types_only_exports_complete_registry_without_reading_any_instance_table(
    source: Connection,
) -> None:
    for table in EXPORT._TABLES.values():
        del source.tables[table]  # Any attempted instance query would fail this fixture.
    snapshot = EXPORT.collect_snapshot(source, source_label="fixture-types", types_only=True)
    checked = SynSnapshot.from_json(EXPORT.snapshot_bytes(snapshot).decode())
    assert checked.catalog_mode == "complete"
    assert len(checked.types) == 2
    assert checked.objects == ()
    assert len(source.queries) == 3
    assert len(merge_types(checked)) == 49
    person = next(record for record in checked.types if record.name == "person")
    assert person.parents == ("syn://type/thing",)
    assert person.properties[0].name == "friend"


@pytest.mark.parametrize("private_key", ["privateData", "private_data"])
@pytest.mark.parametrize("target", ["payload", "metadata"])
def test_private_fields_nested_anywhere_block_the_export(
    source: Connection, private_key: str, target: str
) -> None:
    if target == "payload":
        row = source.tables["things"][0]
        column = "payload_json"
    else:
        row = source.tables["types"][0]
        column = "metadata_json"
    row[column] = row[column][:-1] + ',"nested":[{"' + private_key + '":"must-not-leak"}]}'
    with pytest.raises(EXPORT.ExportError, match="private credential field"):
        EXPORT.collect_snapshot(source, source_label="fixture")


@pytest.mark.parametrize(
    "json_text",
    ['{"duplicate":1,"duplicate":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', "[]"],
)
def test_lossless_public_json_rejects_ambiguous_or_invalid_values(json_text: str) -> None:
    with pytest.raises(EXPORT.ExportError):
        EXPORT._public_json(json_text)


def test_depth_and_size_failures_are_explicit(
    source: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(EXPORT.ExportError, match="nesting"):
        EXPORT._public_json('{"deep":' + "[" * 128 + "0" + "]" * 128 + "}")
    assert EXPORT._public_json('{"braces":"[[[[\\"{}"}') == {"braces": '[[[["{}'}
    monkeypatch.setattr(EXPORT, "MAX_OBJECTS", 1)
    with pytest.raises(EXPORT.ExportError, match="object count"):
        EXPORT.collect_snapshot(source, source_label="fixture")


def test_missing_class_cycle_or_range_rejects_incomplete_native_graph(source: Connection) -> None:
    source.tables["typeParents"].append(
        {"child_ref": "syn://type/thing", "parent_ref": "syn://type/person"}
    )
    with pytest.raises(EXPORT.ExportError, match="cycle"):
        EXPORT.collect_snapshot(source, source_label="fixture")
    source.tables["typeParents"].pop()
    source.tables["typeProperties"][0]["type_ref"] = "syn://type/missing"
    with pytest.raises(EXPORT.ExportError, match="unknown owner or range"):
        EXPORT.collect_snapshot(source, source_label="fixture")


def test_output_replacement_is_atomic_and_limit_failure_preserves_existing_file(
    source: Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = EXPORT.collect_snapshot(source, source_label="fixture")
    output = tmp_path / "snapshot.json"
    output.write_bytes(b"previous completed output")
    monkeypatch.setattr(EXPORT, "MAX_SNAPSHOT_BYTES", 100)
    with pytest.raises(EXPORT.ExportError, match="byte limit"):
        EXPORT.write_snapshot(output, snapshot)
    assert output.read_bytes() == b"previous completed output"
    monkeypatch.setattr(EXPORT, "MAX_SNAPSHOT_BYTES", 64 * 1024 * 1024)
    EXPORT.write_snapshot(output, snapshot)
    assert SynSnapshot.from_json(output.read_text()).source_label == "fixture"
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize("types_only", [False, True])
def test_cli_reads_config_without_source_imports_and_fences_transaction(
    source: Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    types_only: bool,
) -> None:
    config = tmp_path / "syn.toml"
    config.write_text(
        '[database]\nurl="postgresql://user:secret@localhost/syn"\nautoMigrate=true\necho=true\n'
    )
    captured: dict[str, Any] = {}
    engine = SimpleNamespace(connect=lambda: source, dispose=lambda: captured.update(disposed=True))
    sqlalchemy = ModuleType("sqlalchemy")

    def create_engine(url: str, **kwargs: Any) -> Any:
        captured.update(url=url, options=kwargs)
        return engine

    sqlalchemy.create_engine = create_engine  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sqlalchemy", sqlalchemy)
    output = tmp_path / "snapshot.json"
    arguments = ["--config", str(config), "--source-label", "fixture", "--output", str(output)]
    if types_only:
        arguments.append("--types-only")
    assert EXPORT.main(arguments) == 0
    assert captured["url"].startswith("postgresql+psycopg://")
    assert captured["options"] == {"echo": False, "hide_parameters": True}
    assert captured["disposed"]
    assert source.options["isolation_level"] == "REPEATABLE READ"
    assert source.queries[:3] == [
        "SET TRANSACTION READ ONLY",
        "SET LOCAL TIME ZONE 'UTC'",
        "SET LOCAL DateStyle = 'ISO, YMD'",
    ]
    reported = capsys.readouterr()
    assert "secret" not in reported.out + reported.err
    assert "postgresql" not in reported.out + reported.err
    summary = json.loads(reported.out)
    assert "non-type relationships" in summary["omitted"]
    assert summary["types_only"] is types_only
    assert summary["objects"] == (0 if types_only else 9)
    if types_only:
        assert "all instance records" in summary["omitted"]
        assert len(source.queries) == 6  # Three transaction settings and three type reads.


def test_database_errors_never_echo_connection_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SYN_TEST_URL", "postgresql://user:do-not-disclose@localhost/syn")
    sqlalchemy = ModuleType("sqlalchemy")

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("connection failed: user:do-not-disclose@localhost")

    sqlalchemy.create_engine = fail  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sqlalchemy", sqlalchemy)
    with pytest.raises(SystemExit) as error:
        EXPORT.main(
            [
                "--database-url-env",
                "SYN_TEST_URL",
                "--source-label",
                "fixture",
                "--output",
                str(tmp_path / "snapshot.json"),
            ]
        )
    assert error.value.code == 2
    reported = capsys.readouterr()
    assert "do-not-disclose" not in reported.out + reported.err
    assert not (tmp_path / "snapshot.json").exists()
