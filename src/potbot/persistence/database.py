"""Scoped PostgreSQL transactions, migrations, provisioning and command replay.

The application login is deliberately separate from the migration owner. Each
authenticated principal has a PostgreSQL login binding; RLS verifies that binding
with session_user, so changing a custom session setting cannot change identity.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    OperationTimeoutError,
    RevisionConflictError,
    ScopeDeniedError,
    SourceUnavailableError,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.schema import SchemaMigration, application_migrations

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_UNAVAILABLE = "The requested resource is not accessible."


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise InvalidInputError("Database identifiers must be lowercase, bounded SQL identifiers.")
    return value


@contextmanager
def admin_connection(dsn: str, schema: str = "potbot") -> Iterator[Any]:
    """Open an explicit administrative transaction; never used by request paths."""
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    _identifier(schema)
    with psycopg.connect(
        dsn,
        row_factory=dict_row,
        connect_timeout=5,
        options="-c search_path=pg_catalog -c statement_timeout=30000 -c lock_timeout=10000",
    ) as connection:
        connection.execute(
            sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(schema))
        )
        yield connection


def _restricted_login(connection: Any, schema: str, role: str) -> None:
    row = connection.execute(
        "SELECT r.rolsuper, r.rolbypassrls, r.rolcreatedb, r.rolcreaterole, "
        "r.rolreplication, r.rolcanlogin, "
        "pg_catalog.has_schema_privilege(r.oid,n.oid,'CREATE') AS can_create, "
        "EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member=r.oid) AS inherits, "
        "EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
        "WHERE c.relnamespace=n.oid AND c.relowner=r.oid) AS owns_relation, "
        "EXISTS (SELECT 1 FROM pg_catalog.pg_proc f "
        "WHERE f.pronamespace=n.oid AND f.proowner=r.oid) AS owns_function "
        "FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_namespace n "
        "WHERE r.rolname=%s AND n.nspname=%s",
        (role, schema),
    ).fetchone()
    if (
        row is None
        or not row["rolcanlogin"]
        or any(
            row[key]
            for key in (
                "rolsuper",
                "rolbypassrls",
                "rolcreatedb",
                "rolcreaterole",
                "rolreplication",
                "can_create",
                "inherits",
                "owns_relation",
                "owns_function",
            )
        )
    ):
        raise ScopeDeniedError("Application transactions require a restricted database login.")


@dataclass(frozen=True, slots=True)
class Database:
    """Connection factory for authenticated application transactions."""

    dsn: str = field(repr=False)
    schema: str = "potbot"

    def __post_init__(self) -> None:
        _identifier(self.schema)
        if not isinstance(self.dsn, str) or not self.dsn:
            raise InvalidInputError("An explicit application database DSN is required.")

    @contextmanager
    def transaction(self, context: WorkspaceContext) -> Iterator[Any]:
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row

        if not isinstance(context, WorkspaceContext):
            raise ScopeDeniedError(_UNAVAILABLE)
        work_started = False
        try:
            with psycopg.connect(
                self.dsn,
                row_factory=dict_row,
                connect_timeout=5,
                options="-c search_path=pg_catalog -c statement_timeout=30000 -c lock_timeout=10000",
            ) as connection:
                connection.execute(
                    sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                        sql.Identifier(self.schema)
                    )
                )
                login = connection.execute("SELECT session_user AS role").fetchone()
                assert login is not None
                role = login["role"]
                _restricted_login(connection, self.schema, role)
                member = connection.execute(
                    "SELECT 1 FROM principals p JOIN workspace_memberships m "
                    "ON m.principal_id=p.id WHERE p.id=%s AND m.workspace_id=%s "
                    "AND p.database_role=session_user",
                    (context.principal_id, context.workspace_id),
                ).fetchone()
                if member is None:
                    raise ScopeDeniedError(_UNAVAILABLE)
                connection.execute(
                    "SELECT pg_catalog.set_config('potbot.workspace_id',%s,true), "
                    "pg_catalog.set_config('potbot.principal_id',%s,true)",
                    (str(context.workspace_id), str(context.principal_id)),
                )
                work_started = True
                yield connection
        except psycopg.Error as error:
            if error.sqlstate in {"23503", "42501"}:
                raise ScopeDeniedError(_UNAVAILABLE) from None
            if error.sqlstate in {"23505", "55000"}:
                raise RevisionConflictError(
                    "The requested change conflicts with durable state."
                ) from None
            if error.sqlstate in {"23502", "23514", "22P02"}:
                raise InvalidInputError("The request violates the record contract.") from None
            if error.sqlstate in {"57014", "55P03"}:
                raise OperationTimeoutError(
                    "The database operation exceeded its time limit."
                ) from None
            if isinstance(error, psycopg.OperationalError | psycopg.InterfaceError):
                if work_started:
                    raise IndeterminateExecutionError(
                        "The database transaction outcome needs reconciliation."
                    ) from None
                raise SourceUnavailableError("The application database is unavailable.") from None
            raise


def _scope_expression(column: str = "workspace_id") -> str:
    # Identifiers are installation-owned constants, never model-controlled SQL.
    return (
        f"{column}=NULLIF(pg_catalog.current_setting('potbot.workspace_id',true),'')::uuid "
        "AND EXISTS (SELECT 1 FROM workspace_memberships m JOIN principals p "
        "ON p.id=m.principal_id WHERE m.workspace_id=" + column + " "
        "AND p.database_role=session_user "
        "AND p.id=NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid)"
    )


def _apply_security(connection: Any, migration: SchemaMigration) -> None:
    from psycopg import sql

    if migration.version == 1:
        for table in ("principals", "workspace_memberships", "workspaces"):
            connection.execute(
                sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(sql.Identifier(table))
            )
            connection.execute(
                sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(sql.Identifier(table))
            )
        connection.execute(
            "CREATE POLICY principal_identity ON principals FOR SELECT "
            "USING (database_role=session_user)"
        )
        connection.execute(
            "CREATE POLICY own_memberships ON workspace_memberships FOR SELECT "
            "USING (EXISTS (SELECT 1 FROM principals p "
            "WHERE p.id=principal_id AND p.database_role=session_user))"
        )
        # Fully qualify the outer workspace column to prevent a subquery's id
        # binding from accidentally converting this into an unrelated test.
        expression = _scope_expression("workspaces.id")
        connection.execute(
            sql.SQL("CREATE POLICY workspace_scope ON workspaces FOR SELECT USING ({})").format(
                sql.SQL(expression)
            )
        )
    for table in migration.workspace_tables:
        _identifier(table)
        connection.execute(
            sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(sql.Identifier(table))
        )
        connection.execute(
            sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(sql.Identifier(table))
        )
        expression = _scope_expression(f"{table}.workspace_id")
        connection.execute(
            sql.SQL("CREATE POLICY workspace_scope ON {} USING ({}) WITH CHECK ({})").format(
                sql.Identifier(table), sql.SQL(expression), sql.SQL(expression)
            )
        )
    for table in migration.append_only_tables:
        _identifier(table)
        connection.execute(
            sql.SQL(
                "CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON {} "
                "FOR EACH ROW EXECUTE FUNCTION reject_immutable_change()"
            ).format(sql.Identifier(table))
        )


def _grant(connection: Any, schema: str, role: str, migrations: Sequence[SchemaMigration]) -> None:
    from psycopg import sql

    _identifier(role)
    _restricted_login(connection, schema, role)
    role_id = sql.Identifier(role)
    connection.execute(
        sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(
            sql.Identifier(schema), role_id
        )
    )
    connection.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), role_id)
    )
    connection.execute(
        sql.SQL("GRANT SELECT ON principals,workspace_memberships,workspaces TO {}").format(role_id)
    )
    for migration in migrations:
        for table in migration.workspace_tables:
            grants = (
                "SELECT, INSERT"
                if table in migration.append_only_tables
                else "SELECT, INSERT, UPDATE"
            )
            connection.execute(
                sql.SQL("GRANT {} ON {} TO {}").format(
                    sql.SQL(grants), sql.Identifier(table), role_id
                )
            )


def _seed_catalog(connection: Any, workspace_id: UUID) -> None:
    # Lazy import avoids a module cycle and does not construct deployment claims
    # from family names. These are unqualified catalog identities only.
    from potbot.models.registry import DEFAULT_MODELS

    for canonical, role in DEFAULT_MODELS:
        connection.execute(
            "INSERT INTO model_catalog(workspace_id,id,canonical_model,role) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (workspace_id,canonical_model) DO NOTHING",
            (workspace_id, uuid5(NAMESPACE_URL, canonical), canonical, role),
        )


def migrate(
    admin_dsn: str, schema: str = "potbot", migrations: Sequence[SchemaMigration] | None = None
) -> tuple[int, ...]:
    """Atomically apply a known migration prefix and its security policies.

    Migration statements and their scoped/immutable classifications share one
    checksum. Unknown future versions, edited history and version gaps fail.
    """
    from psycopg import sql

    _identifier(schema)
    selected = tuple(application_migrations() if migrations is None else migrations)
    if not selected or tuple(item.version for item in selected) != tuple(
        range(1, len(selected) + 1)
    ):
        raise InvalidInputError("Migrations must be a nonempty, ordered, contiguous revision set.")
    with admin_connection(admin_dsn, schema) as connection:
        lock = int.from_bytes(
            hashlib.sha256(f"potbot:migrate:{schema}".encode()).digest()[:8], "big", signed=True
        )
        connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (lock,))
        connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        connection.execute(
            sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema))
        )
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
        extension = connection.execute(
            "SELECT n.nspname FROM pg_catalog.pg_extension e "
            "JOIN pg_catalog.pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'"
        ).fetchone()
        if extension is None or extension["nspname"] != "public":
            raise InvalidInputError("The vector extension must be installed in the public schema.")
        connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version integer PRIMARY KEY,
            name text NOT NULL,
            checksum text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
        )""")
        applied = connection.execute(
            "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        if len(applied) > len(selected) or any(
            row["version"] != item.version
            or row["name"] != item.name
            or row["checksum"] != item.checksum
            for row, item in zip(applied, selected, strict=False)
        ):
            raise RevisionConflictError(
                "Applied migration history does not match the supplied revisions."
            )
        for migration in selected[len(applied) :]:
            for statement in migration.statements:
                connection.execute(statement)
            _apply_security(connection, migration)
            connection.execute(
                "INSERT INTO schema_migrations(version,name,checksum) VALUES (%s,%s,%s)",
                (migration.version, migration.name, migration.checksum),
            )
        connection.execute(
            sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM PUBLIC").format(
                sql.Identifier(schema)
            )
        )
        if len(selected) >= 4:
            for workspace in connection.execute("SELECT id FROM workspaces").fetchall():
                _seed_catalog(connection, workspace["id"])
        for row in connection.execute("SELECT database_role FROM principals").fetchall():
            _grant(connection, schema, row["database_role"], selected)
    return tuple(item.version for item in selected)


def grant_application_role(admin_dsn: str, schema: str, role: str) -> None:
    with admin_connection(admin_dsn, schema) as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        revisions = application_migrations()[: len(versions)]
        _grant(connection, schema, role, revisions)


def provision_workspace(
    admin_dsn: str, schema: str, name: str, workspace_id: UUID | None = None
) -> UUID:
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
        raise InvalidInputError("Workspace name must contain 1 to 200 characters.")
    identity = workspace_id or uuid4()
    with admin_connection(admin_dsn, schema) as connection:
        connection.execute("INSERT INTO workspaces(id,name) VALUES (%s,%s)", (identity, name))
        catalog = connection.execute("SELECT to_regclass('model_catalog') AS relation").fetchone()
        if catalog is not None and catalog["relation"] is not None:
            _seed_catalog(connection, identity)
    return identity


def provision_principal(
    admin_dsn: str,
    schema: str,
    database_role: str,
    workspace_id: UUID,
    principal_id: UUID | None = None,
) -> WorkspaceContext:
    """Bind an existing restricted login to a principal and one workspace."""
    _identifier(database_role)
    with admin_connection(admin_dsn, schema) as connection:
        _restricted_login(connection, schema, database_role)
        row = connection.execute(
            "SELECT id FROM principals WHERE database_role=%s", (database_role,)
        ).fetchone()
        identity = row["id"] if row else (principal_id or uuid4())
        if row and principal_id is not None and identity != principal_id:
            raise RevisionConflictError("The login is already bound to a different principal.")
        if row is None:
            connection.execute(
                "INSERT INTO principals(id,database_role) VALUES (%s,%s)", (identity, database_role)
            )
        connection.execute(
            "INSERT INTO workspace_memberships(workspace_id,principal_id) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING",
            (workspace_id, identity),
        )
    grant_application_role(admin_dsn, schema, database_role)
    return WorkspaceContext(workspace_id, identity)


def idempotent_command(
    connection: Any,
    context: WorkspaceContext,
    operation: str,
    key: str,
    request: Any,
    callback: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Execute a command once per scoped key or return its committed result.

    The callback, domain writes, events and result commit together. This is a
    database command guarantee and makes no exactly-once external-effect claim.
    """
    from psycopg.types.json import Jsonb

    if not isinstance(operation, str) or not 1 <= len(operation) <= 100:
        raise InvalidInputError("Operation must contain 1 to 100 characters.")
    if not isinstance(key, str) or not 1 <= len(key) <= 128:
        raise InvalidInputError("An idempotency key of 1 to 128 characters is required.")
    active = connection.execute(
        "SELECT current_setting('potbot.workspace_id',true) AS workspace_id, "
        "current_setting('potbot.principal_id',true) AS principal_id"
    ).fetchone()
    if (
        active is None
        or active["workspace_id"] != str(context.workspace_id)
        or active["principal_id"] != str(context.principal_id)
    ):
        raise ScopeDeniedError(_UNAVAILABLE)
    request_hash = content_hash(request)
    lock_payload = canonical_json([str(context.workspace_id), operation, key]).encode()
    lock = int.from_bytes(hashlib.sha256(lock_payload).digest()[:8], "big", signed=True)
    connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (lock,))
    row = connection.execute(
        "SELECT request_hash,result FROM command_results "
        "WHERE workspace_id=%s AND operation=%s AND idempotency_key=%s",
        (context.workspace_id, operation, key),
    ).fetchone()
    if row is not None:
        if row["request_hash"] != request_hash:
            raise RevisionConflictError("This idempotency key was used for a different request.")
        result: dict[str, Any] = row["result"]
        return result
    created = callback()
    if not isinstance(created, dict):
        raise InvalidInputError("A durable command result must be a JSON object.")
    durable: dict[str, Any] = json.loads(canonical_json(created))
    connection.execute(
        "INSERT INTO command_results(workspace_id,id,operation,idempotency_key,request_hash,result,principal_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (
            context.workspace_id,
            uuid4(),
            operation,
            key,
            request_hash,
            Jsonb(durable),
            context.principal_id,
        ),
    )
    return durable


class RepositoryStore:
    """Scoped repository registration and lookup through the same service context."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        context: WorkspaceContext,
        *,
        name: str,
        source_uri: str,
        repository_id: UUID | None = None,
    ) -> dict[str, Any]:
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
            raise InvalidInputError("Repository name must contain 1 to 200 characters.")
        if not isinstance(source_uri, str) or not 1 <= len(source_uri.strip()) <= 2000:
            raise InvalidInputError("Repository source must contain 1 to 2000 characters.")
        identity = repository_id or uuid4()
        with self.database.transaction(context) as connection:
            row = connection.execute(
                "INSERT INTO repositories(workspace_id,id,name,source_uri) VALUES (%s,%s,%s,%s) RETURNING *",
                (context.workspace_id, identity, name, source_uri),
            ).fetchone()
            result: dict[str, Any] = dict(row)
        return result

    def get(self, context: WorkspaceContext, repository_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as connection:
            row = connection.execute(
                "SELECT * FROM repositories WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, repository_id),
            ).fetchone()
            if row is None:
                raise ScopeDeniedError(_UNAVAILABLE)
            result: dict[str, Any] = dict(row)
        return result
