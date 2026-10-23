"""Disposable application databases with genuine unprivileged workspace logins."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import pytest

from potbot.domain.auth import WorkspaceContext
from potbot.persistence.database import (
    Database,
    RepositoryStore,
    configure_workspace_sandbox,
    migrate,
    provision_principal,
    provision_workspace,
)
from potbot.persistence.harness import HarnessError, TestDatabaseConfig


@dataclass(frozen=True)
class AppEnvironment:
    admin_dsn: str
    schema: str
    db: Database
    context: WorkspaceContext
    other_db: Database
    other_context: WorkspaceContext
    repository_id: UUID
    other_repository_id: UUID
    artifact_root: Path


@pytest.fixture
def app_env(tmp_path: Path) -> Iterator[AppEnvironment]:
    # Import only when selected: unit suite collection works without PostgreSQL.
    import psycopg
    from psycopg import sql

    try:
        config = TestDatabaseConfig.from_env()
    except HarnessError as error:
        pytest.fail(f"PostgreSQL application fixture unavailable: {error}", pytrace=False)
    parsed = urlsplit(config.dsn)
    suffix = uuid4().hex
    database_name = f"potbot_test_{suffix}"
    admin_dsn = urlunsplit(parsed._replace(path=f"/{database_name}"))
    roles = (f"potbot_app_{suffix}_a", f"potbot_app_{suffix}_b")
    password = secrets.token_hex(24)
    created_roles: list[str] = []
    database_created = False
    try:
        with psycopg.connect(config.dsn, autocommit=True, connect_timeout=5) as connection:
            connection.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                    sql.Identifier(database_name)
                )
            )
            database_created = True
            for role in roles:
                connection.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOREPLICATION NOBYPASSRLS NOINHERIT"
                    ).format(sql.Identifier(role), sql.Literal(password))
                )
                created_roles.append(role)
        migrate(admin_dsn)
        workspace = provision_workspace(admin_dsn, "potbot", "workspace-a")
        other_workspace = provision_workspace(admin_dsn, "potbot", "workspace-b")
        # The API/CLI requires an explicitly registered root; exercise that
        # production boundary rather than bypassing it in integration tests.
        primary_sandbox = tmp_path / "primary-workspace"
        configure_workspace_sandbox(admin_dsn, "potbot", workspace, primary_sandbox)
        configure_workspace_sandbox(
            admin_dsn, "potbot", other_workspace, tmp_path / "other-workspace"
        )
        context = provision_principal(admin_dsn, "potbot", roles[0], workspace)
        other_context = provision_principal(admin_dsn, "potbot", roles[1], other_workspace)
        authority = f"{roles[0]}:{password}@{parsed.hostname}:{parsed.port}"
        other_authority = f"{roles[1]}:{password}@{parsed.hostname}:{parsed.port}"
        db = Database(urlunsplit(parsed._replace(netloc=authority, path=f"/{database_name}")))
        other_db = Database(
            urlunsplit(parsed._replace(netloc=other_authority, path=f"/{database_name}"))
        )
        repository = RepositoryStore(db).create(context, name="fixture", source_uri="fixture://one")
        other_repository = RepositoryStore(other_db).create(
            other_context, name="other-fixture", source_uri="fixture://two"
        )
        yield AppEnvironment(
            admin_dsn,
            "potbot",
            db,
            context,
            other_db,
            other_context,
            repository["id"],
            other_repository["id"],
            primary_sandbox / "artifacts" / "objects",
        )
    finally:
        with psycopg.connect(config.dsn, autocommit=True, connect_timeout=5) as connection:
            if database_created:
                # This name is generated and created in this scope. No user
                # database name or arbitrary cascade cleanup is accepted.
                connection.execute(
                    sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name))
                )
            for role in created_roles:
                connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
