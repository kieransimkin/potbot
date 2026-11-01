"""Resolve the ordinary operator's restricted login, never administrative credentials."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from potbot.context.projection import ContextProjectionManager
from potbot.context.runtime_integration import context_parent
from potbot.context.service import WorkspaceContextService
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError


def default_context_manager(
    paths: Any,
    *,
    principal_id: UUID | None = None,
    parent: Path | None = None,
    schema: str | None = None,
    shared_skills: Path | None = None,
) -> ContextProjectionManager:
    """Authenticate against session_user before allocating the private projection root.

    Native service children inherit their existing service configuration's scope.
    Standalone operators use --context-schema/POTBOT_CONTEXT_SCHEMA or 'potbot'.
    No agent-supplied UUID, DSN or directory enters this path.
    """
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    from potbot.credentials import resolve_postgres_dsn
    from potbot.persistence.database import Database, _identifier, _restricted_login

    if os.environ.get("POTBOT_OPERATOR_REQUIRED") == "1":
        from potbot.service import ServiceConfig, config_path

        config = ServiceConfig.load(config_path())
        if UUID(config.workspace_id) != paths.workspace_id:
            raise ScopeDeniedError("Operator service scope differs from manifest")
        if schema is not None and schema != config.schema:
            raise ScopeDeniedError("Operator service schema differs from configuration")
        if principal_id is not None and principal_id != UUID(config.principal_id):
            raise ScopeDeniedError("Operator service principal differs from configuration")
        principal_id, schema = UUID(config.principal_id), config.schema
    schema = _identifier(schema or os.environ.get("POTBOT_CONTEXT_SCHEMA", "potbot"))
    database = Database(resolve_postgres_dsn(), schema)
    with psycopg.connect(
        database.dsn,
        row_factory=dict_row,
        connect_timeout=5,
        options="-c search_path=pg_catalog -c statement_timeout=30000 -c lock_timeout=10000",
    ) as conn:
        conn.execute(
            sql.SQL("SET LOCAL search_path TO {},pg_catalog").format(sql.Identifier(schema))
        )
        login = conn.execute("SELECT session_user AS role").fetchone()
        _restricted_login(conn, schema, login["role"])
        identities = conn.execute(
            "SELECT p.id FROM principals p JOIN workspace_memberships m ON m.principal_id=p.id "
            "WHERE p.database_role=session_user AND m.workspace_id=%s",
            (paths.workspace_id,),
        ).fetchall()
    if len(identities) != 1 or (principal_id is not None and identities[0]["id"] != principal_id):
        raise ScopeDeniedError("Operator login has no matching workspace identity")
    principal = WorkspaceContext(paths.workspace_id, identities[0]["id"])
    service = WorkspaceContextService(database)
    # Verify source state and migration readiness before any filesystem write.
    service.read_triple(principal, "sandbox", str(principal.workspace_id))
    with database.transaction(principal) as conn:
        conn.execute("SELECT 1 FROM workspace_context_sources LIMIT 0")
    from dataclasses import replace

    runtime = replace(paths.runtime(), shared_skills=shared_skills)
    if parent is None:
        parent = context_parent(
            SimpleNamespace(path=paths.controller_ledger), runtime, paths.workspace_id
        )
    else:
        parent = Path(parent)
        # ContextProjectionManager validates ownership/mode; also reject overlap
        # before it can publish a snapshot.
        for area in (
            runtime.rootfs,
            runtime.home,
            runtime.model_staging,
            runtime.shared_models,
            runtime.shared_tools,
            runtime.shared_skills,
            runtime.ai_config_dir,
            runtime.ai_history_dir,
        ):
            if area is not None:
                other = Path(area).resolve(strict=True)
                if parent == other or parent.is_relative_to(other) or other.is_relative_to(parent):
                    raise ScopeDeniedError("Context storage overlaps a guest mount")
    from potbot.context.skill_projection import _directory

    _directory(parent)
    if parent.stat().st_mode & 0o077:
        raise ScopeDeniedError("Operator context directory must be private")
    return ContextProjectionManager(service=service, principal=principal, parent=parent)
