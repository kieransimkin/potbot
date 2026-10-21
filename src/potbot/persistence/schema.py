"""Application schema revisions, independent of the disposable Phase 1 harness."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from typing import Any

from potbot.domain.serialization import content_hash


@dataclass(frozen=True, slots=True)
class SchemaMigration:
    version: int
    name: str
    statements: tuple[str, ...]
    workspace_tables: tuple[str, ...] = ()
    append_only_tables: tuple[str, ...] = ()

    @property
    def checksum(self) -> str:
        return content_hash(
            {
                "version": self.version,
                "name": self.name,
                "statements": self.statements,
                "workspace_tables": self.workspace_tables,
                "append_only_tables": self.append_only_tables,
            }
        )


BASE_MIGRATION = SchemaMigration(
    1,
    "scoped_identity_tasks_commands",
    (
        """CREATE TABLE principals (
            id uuid PRIMARY KEY,
            database_role name NOT NULL UNIQUE,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp()
        )""",
        """CREATE TABLE workspaces (
            id uuid PRIMARY KEY,
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp()
        )""",
        """CREATE TABLE workspace_memberships (
            workspace_id uuid NOT NULL REFERENCES workspaces(id),
            principal_id uuid NOT NULL REFERENCES principals(id),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (workspace_id, principal_id)
        )""",
        "CREATE INDEX memberships_principal ON workspace_memberships(principal_id,workspace_id)",
        """CREATE TABLE repositories (
            workspace_id uuid NOT NULL REFERENCES workspaces(id),
            id uuid NOT NULL,
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            source_uri text NOT NULL CHECK (length(source_uri) BETWEEN 1 AND 2000),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (workspace_id, id)
        )""",
        """CREATE TABLE episodes (
            workspace_id uuid NOT NULL REFERENCES workspaces(id),
            id uuid NOT NULL,
            repository_id uuid NOT NULL,
            deliverable_type text NOT NULL CHECK (deliverable_type IN ('patch', 'investigation')),
            status text NOT NULL DEFAULT 'submitted' CHECK (status IN (
                'submitted', 'initializing', 'running', 'paused', 'blocked',
                'verified', 'partial', 'budget_exhausted', 'cancelled', 'failed')),
            active_contract_id uuid,
            update_token bigint NOT NULL DEFAULT 0 CHECK (update_token >= 0),
            status_reason text,
            blocked_on text,
            previous_status text,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (workspace_id, id),
            FOREIGN KEY (workspace_id, repository_id) REFERENCES repositories(workspace_id,id)
        )""",
        "CREATE INDEX episodes_workspace_status ON episodes(workspace_id,status,created_at)",
        """CREATE TABLE command_results (
            workspace_id uuid NOT NULL REFERENCES workspaces(id),
            id uuid NOT NULL,
            operation text NOT NULL,
            idempotency_key text NOT NULL,
            request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
            result jsonb NOT NULL,
            principal_id uuid NOT NULL,
            recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (workspace_id,id),
            UNIQUE (workspace_id, operation, idempotency_key),
            FOREIGN KEY (workspace_id,principal_id)
                REFERENCES workspace_memberships(workspace_id,principal_id)
        )""",
        """CREATE FUNCTION reject_immutable_change() RETURNS trigger
            LANGUAGE plpgsql SET search_path = pg_catalog AS $$
            BEGIN
                RAISE EXCEPTION 'immutable record cannot be changed' USING ERRCODE = '55000';
            END; $$""",
    ),
    ("repositories", "episodes", "command_results"),
    ("repositories", "command_results"),
)


def application_migrations(*, lineage: str = "main") -> tuple[SchemaMigration, ...]:
    """All shipped revisions in dependency order; missing modules are failures."""
    migrations = [BASE_MIGRATION]
    if lineage not in {"main", "phase3-preview"}:
        raise ValueError("Unknown application migration lineage")
    extensions = (
        (
            (6, "syn_schema"),
            (7, "repository_schema"),
            (8, "semantic_schema"),
            (9, "execution_schema"),
        )
        if lineage == "main"
        else (
            (6, "repository_schema"),
            (7, "semantic_schema"),
            (8, "execution_schema"),
            (9, "syn_schema"),
        )
    )
    for version, module_name in (
        (2, "evidence_schema"),
        (3, "task_schema"),
        (4, "model_schema"),
        (5, "named_vector_schema"),
        *extensions,
        (10, "phase4_schema"),
    ):
        module = import_module(f"potbot.persistence.{module_name}")
        migrations.append(
            SchemaMigration(
                version,
                module_name,
                tuple(module.MIGRATION_STATEMENTS),
                tuple(module.WORKSPACE_TABLES),
                tuple(module.APPEND_ONLY_TABLES),
            )
        )
    return tuple(migrations)


def migrations_for_history(applied: Sequence[Mapping[str, Any]]) -> tuple[SchemaMigration, ...]:
    """Join both published pre-Phase-4 histories without rewriting either ledger.

    GitHub shipped SYN as revision 6 while the Phase 3 ZIP independently shipped
    repository/semantic/execution revisions 6–8. Each known history reaches the
    same schema at revision 9. The caller still checks every original checksum.
    """
    legacy = len(applied) >= 6 and applied[5].get("name") == "repository_schema"
    return application_migrations(lineage="phase3-preview" if legacy else "main")
