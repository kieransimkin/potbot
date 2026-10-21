"""Disposable, ownership-checked PostgreSQL integration fixtures.

Run ``python -m potbot.persistence.harness --help`` for the operator CLI.
This module never reads DATABASE_URL. Its dedicated test DSN must name a local
test database, and destructive operations also require persistent ownership
markers. The database driver is imported only when a database command runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4, uuid5

DSN_ENV = "POTBOT_TEST_DSN"
TEST_USER = "potbot_test"
METADATA_SCHEMA = "potbot_harness"
DATABASE_MARKER = "potbot-disposable-test-database-v1"
PGVECTOR_VERSION = "0.8.7"
_BOOTSTRAP_LOCK = 730018450829
_DATABASE_PATTERN = re.compile(r"potbot_test(?:_[a-z0-9]{1,32})?\Z")
_TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class HarnessError(RuntimeError):
    """An operator-facing failure whose message contains no connection secret."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TestDatabaseConfig:
    """An explicitly credentialed local test target, never a production default."""

    __test__ = False

    dsn: str = field(repr=False)
    host: str = field(init=False)
    port: int = field(init=False)
    database: str = field(init=False)

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.dsn)
            host = parsed.hostname
            port = parsed.port
            username = unquote(parsed.username or "")
            password = unquote(parsed.password or "")
            database = unquote(parsed.path.removeprefix("/"))
        except (TypeError, ValueError):
            raise HarnessError(
                "unsafe_target", "The test DSN is not a valid PostgreSQL URI."
            ) from None
        if (
            parsed.scheme not in {"postgresql", "postgres"}
            or host not in {"localhost", "127.0.0.1", "::1"}
            or port is None
            or not 1 <= port <= 65535
            or username != TEST_USER
            or not password
            or not _DATABASE_PATTERN.fullmatch(database)
            or parsed.query
            or parsed.fragment
            or any(character.isspace() for character in self.dsn)
        ):
            raise HarnessError(
                "unsafe_target",
                "Use an explicit PostgreSQL URI with a loopback host, port, password, "
                "potbot_test user, and potbot_test database "
                "(or a potbot_test_<lowercase alphanumeric> fixture database). "
                "URI options are not accepted.",
            )
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "port", port)
        object.__setattr__(self, "database", database)

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> TestDatabaseConfig:
        environment = os.environ if environment is None else environment
        dsn = environment.get(DSN_ENV)
        if not dsn:
            raise HarnessError(
                "missing_test_dsn",
                f"Set {DSN_ENV} to the dedicated disposable PostgreSQL fixture URI.",
            )
        return cls(dsn)


@dataclass(frozen=True)
class RunHandle:
    """A durable capability for one test schema; no database credential is stored."""

    database_id: UUID
    run_id: UUID
    schema_name: str
    ownership_token: str = field(repr=False)
    created_at: datetime
    format_version: int = 1

    def __post_init__(self) -> None:
        if (
            type(self.format_version) is not int
            or self.format_version != 1
            or not isinstance(self.database_id, UUID)
            or not isinstance(self.run_id, UUID)
            or self.schema_name != f"potbot_test_{self.run_id.hex}"
            or not isinstance(self.ownership_token, str)
            or not _TOKEN_PATTERN.fullmatch(self.ownership_token)
            or not isinstance(self.created_at, datetime)
            or self.created_at.tzinfo is None
            or self.created_at.utcoffset() is None
        ):
            raise HarnessError(
                "invalid_manifest", "The run manifest has an invalid identity or format."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "database_id": str(self.database_id),
            "run_id": str(self.run_id),
            "schema_name": self.schema_name,
            "ownership_token": self.ownership_token,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RunHandle:
        try:
            if (
                set(value)
                != {
                    "format_version",
                    "database_id",
                    "run_id",
                    "schema_name",
                    "ownership_token",
                    "created_at",
                }
                or type(value["format_version"]) is not int
            ):
                raise ValueError
            return cls(
                database_id=UUID(value["database_id"]),
                run_id=UUID(value["run_id"]),
                schema_name=value["schema_name"],
                ownership_token=value["ownership_token"],
                created_at=datetime.fromisoformat(value["created_at"]),
                format_version=value["format_version"],
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise HarnessError(
                "invalid_manifest", "The run manifest has an invalid identity or format."
            ) from None


def write_manifest(path: Path, handle: RunHandle) -> None:
    """Persist identity before allocation can commit; never overwrite a manifest."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(handle.to_dict(), output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        raise HarnessError(
            "manifest_write_failed",
            "Cannot create the run manifest; choose a new path in a writable directory.",
        ) from None


def read_manifest(path: Path) -> RunHandle:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise HarnessError("invalid_manifest", "Cannot read a valid run manifest.") from None
    if not isinstance(value, dict):
        raise HarnessError("invalid_manifest", "The run manifest must contain an object.")
    return RunHandle.from_dict(value)


def _driver() -> Any:
    try:
        import psycopg
    except ImportError:
        raise HarnessError(
            "driver_unavailable", "Install the project's postgres extra to run PostgreSQL commands."
        ) from None
    return psycopg


@contextmanager
def _connection(config: TestDatabaseConfig) -> Iterator[Any]:
    driver = _driver()
    try:
        connection = driver.connect(
            config.dsn,
            # libpq treats hostaddr independently from host. Pin it explicitly
            # so PGHOSTADDR or a service file cannot redirect test credentials.
            hostaddr="::1" if config.host == "::1" else "127.0.0.1",
            autocommit=True,
            connect_timeout=3,
            application_name="potbot_test_harness",
            options="-c search_path=pg_catalog -c statement_timeout=15000 -c lock_timeout=5000",
        )
    except driver.Error:
        raise HarnessError(
            "database_unavailable",
            "Cannot connect to the dedicated test database. Check the fixture service and test credentials.",
        ) from None
    try:
        with connection:
            identity = connection.execute("SELECT current_database(), current_user").fetchone()
            if identity != (config.database, TEST_USER):
                raise HarnessError(
                    "unsafe_target", "The server identity does not match the dedicated test target."
                )
            yield connection
    except driver.Error as error:
        code = error.sqlstate or "unavailable"
        raise HarnessError(
            "database_operation_failed", f"The test database operation failed (SQLSTATE {code})."
        ) from None


def _relation_exists(connection: Any, schema: str, relation: str) -> bool:
    return bool(
        connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s AND c.relkind = 'r')",
            (schema, relation),
        ).fetchone()[0]
    )


def _schema_owner(connection: Any, schema: str) -> str | None:
    row = connection.execute(
        "SELECT pg_catalog.pg_get_userbyid(nspowner) FROM pg_catalog.pg_namespace WHERE nspname = %s",
        (schema,),
    ).fetchone()
    return row[0] if row else None


def _guard_database(connection: Any, database_id: UUID | None = None) -> UUID:
    if _schema_owner(connection, METADATA_SCHEMA) != TEST_USER or not _relation_exists(
        connection, METADATA_SCHEMA, "database_identity"
    ):
        raise HarnessError(
            "missing_database_marker",
            "The database lacks the expected disposable-test marker; refusing the operation.",
        )
    rows = connection.execute(
        "SELECT marker, database_id FROM potbot_harness.database_identity"
    ).fetchall()
    if len(rows) != 1 or rows[0][0] != DATABASE_MARKER or not isinstance(rows[0][1], UUID):
        raise HarnessError(
            "missing_database_marker",
            "The database lacks the expected disposable-test marker; refusing the operation.",
        )
    actual_id: UUID = rows[0][1]
    if database_id is not None and actual_id != database_id:
        raise HarnessError(
            "database_identity_mismatch", "The manifest belongs to a different disposable database."
        )
    return actual_id


def _check_extension(connection: Any) -> str:
    row = connection.execute(
        "SELECT e.extversion, n.nspname FROM pg_catalog.pg_extension e "
        "JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'vector'"
    ).fetchone()
    if row != (PGVECTOR_VERSION, "public"):
        raise HarnessError(
            "vector_unavailable",
            f"The fixture requires pgvector {PGVECTOR_VERSION} in the public schema.",
        )
    return PGVECTOR_VERSION


def bootstrap(config: TestDatabaseConfig) -> UUID:
    """Stamp a fresh database and activate pgvector, or verify an existing stamp."""
    with _connection(config) as connection, connection.transaction():
        connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (_BOOTSTRAP_LOCK,))
        if _schema_owner(connection, METADATA_SCHEMA) is not None:
            database_id = _guard_database(connection)
            _check_extension(connection)
            return database_id
        nonempty = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace "
            "WHERE nspname NOT IN ('public', 'information_schema', 'pg_catalog', 'pg_toast') "
            "AND nspname !~ '^pg_(toast_)?temp_[0-9]+$') "
            # Namespace dependencies cover tables, functions, types, collations,
            # operators, text-search objects, and other schema object classes.
            "OR EXISTS (SELECT 1 FROM pg_catalog.pg_depend d "
            "JOIN pg_catalog.pg_namespace n ON n.oid = d.refobjid "
            "WHERE d.refclassid = 'pg_catalog.pg_namespace'::regclass AND n.nspname = 'public') "
            "OR EXISTS (SELECT 1 FROM pg_catalog.pg_extension WHERE extname <> 'plpgsql') "
            "OR EXISTS (SELECT 1 FROM pg_catalog.pg_largeobject_metadata) "
            "OR EXISTS (SELECT 1 FROM pg_catalog.pg_event_trigger) "
            "OR EXISTS (SELECT 1 FROM pg_catalog.pg_foreign_data_wrapper) "
            "OR EXISTS (SELECT 1 FROM pg_catalog.pg_foreign_server) "
            "OR EXISTS (SELECT 1 FROM pg_catalog.pg_publication) "
            "OR EXISTS (SELECT 1 FROM pg_catalog.pg_subscription WHERE subdbid = "
            "(SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database())) "
            "OR EXISTS (SELECT 1 FROM pg_catalog.pg_default_acl)"
        ).fetchone()[0]
        if nonempty:
            raise HarnessError(
                "nonempty_unmarked_database",
                "Bootstrap refuses an unmarked database containing user objects.",
            )
        connection.execute("CREATE EXTENSION vector WITH SCHEMA public VERSION '0.8.7'")
        connection.execute("CREATE SCHEMA potbot_harness AUTHORIZATION potbot_test")
        connection.execute(
            "CREATE TABLE potbot_harness.database_identity ("
            "singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton), "
            "marker text NOT NULL, database_id uuid NOT NULL UNIQUE, "
            "created_at timestamptz NOT NULL DEFAULT clock_timestamp())"
        )
        connection.execute(
            "CREATE TABLE potbot_harness.runs ("
            "run_id uuid PRIMARY KEY, database_id uuid NOT NULL, schema_name text NOT NULL UNIQUE, "
            "ownership_token text NOT NULL, created_at timestamptz NOT NULL, "
            "state text NOT NULL CHECK (state IN ('allocated', 'migrated', 'cleaned')), "
            "cleaned_at timestamptz)"
        )
        database_id = uuid4()
        connection.execute(
            "INSERT INTO potbot_harness.database_identity (marker, database_id) VALUES (%s, %s)",
            (DATABASE_MARKER, database_id),
        )
        return database_id


def create_run(config: TestDatabaseConfig, manifest_path: Path | None = None) -> RunHandle:
    """Allocate one isolated schema and two independent persistent owner markers."""
    sql = _driver().sql
    with _connection(config) as connection, connection.transaction():
        database_id = _guard_database(connection)
        run_id = uuid4()
        handle = RunHandle(
            database_id,
            run_id,
            f"potbot_test_{run_id.hex}",
            secrets.token_hex(32),
            datetime.now(UTC),
        )
        if manifest_path is not None:
            write_manifest(manifest_path, handle)
        connection.execute(
            sql.SQL("CREATE SCHEMA {} AUTHORIZATION potbot_test").format(
                sql.Identifier(handle.schema_name)
            )
        )
        connection.execute(
            sql.SQL(
                "CREATE TABLE {}._fixture_identity ("
                "singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton), "
                "database_id uuid NOT NULL, run_id uuid NOT NULL, ownership_token text NOT NULL)"
            ).format(sql.Identifier(handle.schema_name))
        )
        connection.execute(
            sql.SQL(
                "INSERT INTO {}._fixture_identity (database_id, run_id, ownership_token) VALUES (%s, %s, %s)"
            ).format(sql.Identifier(handle.schema_name)),
            (database_id, run_id, handle.ownership_token),
        )
        connection.execute(
            sql.SQL(
                "CREATE TABLE {}.schema_migrations ("
                "version integer PRIMARY KEY CHECK (version > 0), name text NOT NULL, "
                "checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT clock_timestamp())"
            ).format(sql.Identifier(handle.schema_name))
        )
        connection.execute(
            "INSERT INTO potbot_harness.runs "
            "(run_id, database_id, schema_name, ownership_token, created_at, state) "
            "VALUES (%s, %s, %s, %s, %s, 'allocated')",
            (run_id, database_id, handle.schema_name, handle.ownership_token, handle.created_at),
        )
        return handle


def _guard_run(connection: Any, handle: RunHandle, *, allow_cleaned: bool = False) -> str:
    sql = _driver().sql
    _guard_database(connection, handle.database_id)
    row = connection.execute(
        "SELECT database_id, schema_name, ownership_token, created_at, state "
        "FROM potbot_harness.runs WHERE run_id = %s FOR UPDATE",
        (handle.run_id,),
    ).fetchone()
    if row is None or row[:4] != (
        handle.database_id,
        handle.schema_name,
        handle.ownership_token,
        handle.created_at,
    ):
        raise HarnessError(
            "run_identity_mismatch",
            "The run is not registered with the supplied ownership identity.",
        )
    owner = _schema_owner(connection, handle.schema_name)
    if row[4] == "cleaned":
        if allow_cleaned and owner is None:
            return "cleaned"
        raise HarnessError(
            "run_already_cleaned",
            "This run was already cleaned; no further schema operation is allowed.",
        )
    if owner != TEST_USER or not _relation_exists(
        connection, handle.schema_name, "_fixture_identity"
    ):
        raise HarnessError(
            "missing_run_marker",
            "The schema lacks its expected owner or fixture marker; refusing the operation.",
        )
    identities = connection.execute(
        sql.SQL("SELECT database_id, run_id, ownership_token FROM {}._fixture_identity").format(
            sql.Identifier(handle.schema_name)
        )
    ).fetchall()
    if identities != [(handle.database_id, handle.run_id, handle.ownership_token)]:
        raise HarnessError(
            "run_identity_mismatch", "The schema marker does not match the registered run identity."
        )
    state = str(row[4])
    if state not in {"allocated", "migrated"}:
        raise HarnessError(
            "run_identity_mismatch", "The registered run has an invalid lifecycle state."
        )
    return state


@dataclass(frozen=True)
class Migration:
    """A trusted code-owned migration; never accept SQL from an agent or manifest."""

    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        payload = json.dumps([self.version, self.name, self.statements], separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


DEFAULT_MIGRATIONS = (
    Migration(
        1,
        "vector_driver_probe",
        (
            "CREATE TABLE probe_records (record_id uuid PRIMARY KEY, label text NOT NULL, "
            "embedding public.vector(3) NOT NULL)",
        ),
    ),
)


def apply_migrations(
    config: TestDatabaseConfig,
    handle: RunHandle,
    migrations: Sequence[Migration] = DEFAULT_MIGRATIONS,
) -> tuple[int, ...]:
    """Apply the complete ordered migration set in one atomic driver transaction."""
    versions = [migration.version for migration in migrations]
    if any(type(version) is not int or version < 1 for version in versions) or versions != sorted(
        set(versions)
    ):
        raise HarnessError(
            "invalid_migrations", "Migrations must have unique ascending positive integer versions."
        )
    sql = _driver().sql
    with _connection(config) as connection, connection.transaction():
        _guard_run(connection, handle)
        _check_extension(connection)
        connection.execute(
            sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(handle.schema_name))
        )
        existing = dict(
            connection.execute("SELECT version, checksum FROM schema_migrations").fetchall()
        )
        expected = {migration.version: migration for migration in migrations}
        if any(
            version not in expected or expected[version].checksum != checksum
            for version, checksum in existing.items()
        ):
            raise HarnessError(
                "migration_checksum_mismatch",
                "Applied migration history differs from the trusted migration set.",
            )
        for migration in migrations:
            if migration.version in existing:
                continue
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                (migration.version, migration.name, migration.checksum),
            )
        connection.execute(
            "UPDATE potbot_harness.runs SET state = 'migrated' WHERE run_id = %s",
            (handle.run_id,),
        )
        return tuple(versions)


def check_run(config: TestDatabaseConfig, handle: RunHandle) -> dict[str, Any]:
    """Insert and read a real vector through the application driver."""
    sql = _driver().sql
    with _connection(config) as connection, connection.transaction():
        _guard_run(connection, handle)
        extension = _check_extension(connection)
        connection.execute(
            sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(handle.schema_name))
        )
        applied = connection.execute(
            "SELECT version, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        if applied != [(migration.version, migration.checksum) for migration in DEFAULT_MIGRATIONS]:
            raise HarnessError(
                "migration_required",
                "Apply the trusted bootstrap migration before running the vector check.",
            )
        record_id = uuid5(handle.run_id, "driver-vector-probe")
        connection.execute(
            "INSERT INTO probe_records (record_id, label, embedding) VALUES (%s, %s, %s::public.vector) "
            "ON CONFLICT (record_id) DO UPDATE SET label = EXCLUDED.label, embedding = EXCLUDED.embedding",
            (record_id, "phase-1-driver-check", "[1,2,3]"),
        )
        vector, distance = connection.execute(
            "SELECT embedding::text, embedding OPERATOR(public.<->) %s::public.vector "
            "FROM probe_records WHERE record_id = %s",
            ("[1,2,3]", record_id),
        ).fetchone()
        if vector != "[1,2,3]" or distance != 0.0:
            raise HarnessError(
                "vector_check_failed",
                "The vector write/read/distance check returned an unexpected result.",
            )
        return {
            "run_id": str(handle.run_id),
            "schema_name": handle.schema_name,
            "postgres_version": connection.info.server_version,
            "pgvector_version": extension,
            "migration_versions": [row[0] for row in applied],
            "vector": vector,
            "l2_distance": distance,
        }


def cleanup_run(config: TestDatabaseConfig, handle: RunHandle) -> bool:
    """Drop an owned Phase 1 table schema, refusing external dependencies.

    Tables are dropped as one RESTRICT group, followed by DROP SCHEMA RESTRICT.
    Any external dependency or additional unsupported object type aborts and rolls
    back the entire transaction. CASCADE is never used. New migration object types
    must add explicitly scoped cleanup support before they can be introduced.
    """
    driver = _driver()
    sql = driver.sql
    with _connection(config) as connection, connection.transaction():
        state = _guard_run(connection, handle, allow_cleaned=True)
        if state == "cleaned":
            return False
        tables = connection.execute(
            "SELECT c.relname FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind = 'r' AND NOT c.relispartition "
            "ORDER BY c.relname",
            (handle.schema_name,),
        ).fetchall()
        try:
            if tables:
                connection.execute(
                    sql.SQL("DROP TABLE {} RESTRICT").format(
                        sql.SQL(", ").join(
                            sql.Identifier(handle.schema_name, row[0]) for row in tables
                        )
                    )
                )
            connection.execute(
                sql.SQL("DROP SCHEMA {} RESTRICT").format(sql.Identifier(handle.schema_name))
            )
        except driver.errors.DependentObjectsStillExist:
            raise HarnessError(
                "cleanup_scope_conflict",
                "Cleanup refuses external dependencies or unsupported schema objects; all changes were rolled back.",
            ) from None
        connection.execute(
            "UPDATE potbot_harness.runs SET state = 'cleaned', cleaned_at = clock_timestamp() "
            "WHERE run_id = %s",
            (handle.run_id,),
        )
        return True


def list_runs(config: TestDatabaseConfig) -> list[RunHandle]:
    """Recover active ownership handles after an interrupted process."""
    with _connection(config) as connection, connection.transaction():
        database_id = _guard_database(connection)
        rows = connection.execute(
            "SELECT run_id, schema_name, ownership_token, created_at FROM potbot_harness.runs "
            "WHERE database_id = %s AND state <> 'cleaned' ORDER BY created_at, run_id",
            (database_id,),
        ).fetchall()
        return [RunHandle(database_id, *row) for row in rows]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "bootstrap", help="Mark an empty dedicated test database and enable pgvector."
    )
    commands.add_parser("list", help="Recover active run handles from database metadata.")
    for command in ("create", "migrate", "check", "cleanup"):
        command_parser = commands.add_parser(command)
        command_parser.add_argument("--manifest", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        config = TestDatabaseConfig.from_env()
        result: Any
        if arguments.command == "bootstrap":
            result = {"database_id": str(bootstrap(config)), "pgvector_version": PGVECTOR_VERSION}
        elif arguments.command == "list":
            result = {"runs": [handle.to_dict() for handle in list_runs(config)]}
        elif arguments.command == "create":
            handle = create_run(config, arguments.manifest)
            result = {"run_id": str(handle.run_id), "schema_name": handle.schema_name}
        else:
            handle = read_manifest(arguments.manifest)
            if arguments.command == "migrate":
                result = {"migration_versions": list(apply_migrations(config, handle))}
            elif arguments.command == "check":
                result = check_run(config, handle)
            else:
                result = {"dropped": cleanup_run(config, handle), "run_id": str(handle.run_id)}
        print(json.dumps({"ok": True, **result}, sort_keys=True))
        return 0
    except HarnessError as error:
        print(
            json.dumps({"ok": False, "error": error.code, "message": str(error)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
