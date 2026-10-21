"""Real SQL checks for migration, authorization and atomic command guarantees."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    IndeterminateExecutionError,
    RevisionConflictError,
    ScopeDeniedError,
    SourceUnavailableError,
)
from potbot.persistence.database import (
    Database,
    RepositoryStore,
    admin_connection,
    idempotent_command,
    migrate,
    provision_workspace,
)
from potbot.persistence.schema import SchemaMigration, application_migrations

pytestmark = pytest.mark.postgres


def test_migrations_repeat_without_duplicate_tables_and_detect_modified_history(
    app_env: Any,
) -> None:
    revisions = application_migrations()
    expected_versions = tuple(item.version for item in revisions)
    assert migrate(app_env.admin_dsn) == expected_versions
    assert migrate(app_env.admin_dsn) == expected_versions
    changed = replace(revisions[0], statements=(*revisions[0].statements, "SELECT 1"))
    with pytest.raises(RevisionConflictError):
        migrate(app_env.admin_dsn, migrations=(changed, *revisions[1:]))
    with admin_connection(app_env.admin_dsn) as connection:
        assert connection.execute("SELECT count(*) AS total FROM schema_migrations").fetchone()[
            "total"
        ] == len(revisions)


def test_migration_failure_leaves_no_schema_or_ledger_writes(app_env: Any) -> None:
    import psycopg

    revisions = application_migrations()
    bad = SchemaMigration(
        len(revisions) + 1,
        "injected_failure",
        (
            "CREATE TABLE incomplete_migration (id integer)",
            "SELECT 1/0",
        ),
    )
    with pytest.raises(psycopg.errors.DivisionByZero):
        migrate(app_env.admin_dsn, migrations=(*revisions, bad))
    with admin_connection(app_env.admin_dsn) as connection:
        assert (
            connection.execute("SELECT to_regclass('incomplete_migration') AS name").fetchone()[
                "name"
            ]
            is None
        )
        assert connection.execute("SELECT count(*) AS total FROM schema_migrations").fetchone()[
            "total"
        ] == len(revisions)


def test_real_role_is_restricted_and_foreign_repository_link_rolls_back(app_env: Any) -> None:
    episode_id = uuid4()
    with app_env.db.transaction(app_env.context) as connection:
        role = connection.execute(
            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=session_user"
        ).fetchone()
        assert role == {"rolsuper": False, "rolbypassrls": False}
    with pytest.raises(ScopeDeniedError):
        with app_env.db.transaction(app_env.context) as connection:
            connection.execute(
                "INSERT INTO episodes(workspace_id,id,repository_id,deliverable_type) VALUES (%s,%s,%s,'patch')",
                (app_env.context.workspace_id, episode_id, app_env.other_repository_id),
            )
    with app_env.db.transaction(app_env.context) as connection:
        assert (
            connection.execute("SELECT id FROM episodes WHERE id=%s", (episode_id,)).fetchone()
            is None
        )


def test_direct_ids_joins_enumeration_and_forged_settings_are_isolated(app_env: Any) -> None:
    with app_env.db.transaction(app_env.context) as connection:
        assert (
            connection.execute(
                "SELECT id FROM repositories WHERE id=%s", (app_env.other_repository_id,)
            ).fetchall()
            == []
        )
        assert (
            connection.execute(
                "SELECT r.id FROM repositories r JOIN workspaces w "
                "ON r.workspace_id=w.id WHERE w.id=%s",
                (app_env.other_context.workspace_id,),
            ).fetchall()
            == []
        )
        assert len(connection.execute("SELECT * FROM repositories").fetchall()) == 1
        assert len(connection.execute("SELECT * FROM workspaces").fetchall()) == 1
        assert len(connection.execute("SELECT * FROM workspace_memberships").fetchall()) == 1
        connection.execute(
            "SELECT set_config('potbot.workspace_id',%s,true)",
            (str(app_env.other_context.workspace_id),),
        )
        connection.execute(
            "SELECT set_config('potbot.principal_id',%s,true)",
            (str(app_env.other_context.principal_id),),
        )
        assert connection.execute("SELECT * FROM repositories").fetchall() == []
    for identity in (app_env.other_repository_id, uuid4()):
        with pytest.raises(ScopeDeniedError) as failure:
            RepositoryStore(app_env.db).get(app_env.context, identity)
        assert str(failure.value) == "The requested resource is not accessible."
    with pytest.raises(ScopeDeniedError):
        with app_env.db.transaction(app_env.other_context):
            pytest.fail("A forged context was accepted")
    with pytest.raises(ScopeDeniedError):
        with app_env.db.transaction(WorkspaceContext(app_env.context.workspace_id, uuid4())):
            pytest.fail("A forged principal was accepted")


def test_privileged_application_login_is_rejected(app_env: Any) -> None:
    with pytest.raises(ScopeDeniedError, match="restricted database login"):
        with Database(app_env.admin_dsn).transaction(app_env.context):
            pytest.fail("Admin credentials must not run application transactions")


def test_application_table_ownership_is_rejected_even_without_schema_create(app_env: Any) -> None:
    from psycopg import sql

    with app_env.db.transaction(app_env.context) as connection:
        role = connection.execute("SELECT session_user AS role").fetchone()["role"]
    with admin_connection(app_env.admin_dsn) as connection:
        connection.execute(
            sql.SQL("GRANT CREATE ON SCHEMA potbot TO {}").format(sql.Identifier(role))
        )
        connection.execute(
            sql.SQL("ALTER TABLE repositories OWNER TO {}").format(sql.Identifier(role))
        )
        connection.execute(
            sql.SQL("REVOKE CREATE ON SCHEMA potbot FROM {}").format(sql.Identifier(role))
        )
    with pytest.raises(ScopeDeniedError, match="restricted database login"):
        with app_env.db.transaction(app_env.context):
            pytest.fail("An application table owner must not be admitted")


def test_default_model_catalog_is_seeded_on_creation_and_schema_upgrade(app_env: Any) -> None:
    with app_env.db.transaction(app_env.context) as connection:
        catalog = connection.execute("SELECT * FROM model_catalog").fetchall()
        assert len(catalog) == 5
        assert {row["qualification"] for row in catalog} == {"unqualified"}
        assert connection.execute("SELECT * FROM model_deployments").fetchall() == []
    assert migrate(app_env.admin_dsn, "upgrade", application_migrations()[:1]) == (1,)
    workspace = provision_workspace(app_env.admin_dsn, "upgrade", "existing-workspace")
    assert migrate(app_env.admin_dsn, "upgrade") == tuple(
        item.version for item in application_migrations()
    )
    with admin_connection(app_env.admin_dsn, "upgrade") as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS total FROM model_catalog WHERE workspace_id=%s", (workspace,)
            ).fetchone()["total"]
            == 5
        )


def test_connection_loss_after_write_reports_indeterminate_without_driver_details(
    app_env: Any,
) -> None:
    episode_id = uuid4()
    with pytest.raises(IndeterminateExecutionError) as failure:
        with app_env.db.transaction(app_env.context) as connection:
            backend = connection.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            connection.execute(
                "INSERT INTO episodes(workspace_id,id,repository_id,deliverable_type) VALUES (%s,%s,%s,'patch')",
                (app_env.context.workspace_id, episode_id, app_env.repository_id),
            )
            with admin_connection(app_env.admin_dsn) as admin:
                assert admin.execute(
                    "SELECT pg_terminate_backend(%s) AS stopped", (backend,)
                ).fetchone()["stopped"]
            connection.execute("SELECT 1")
    assert str(failure.value) == "The database transaction outcome needs reconciliation."
    with app_env.db.transaction(app_env.context) as connection:
        # The server can subsequently confirm rollback. The interrupted caller
        # still correctly reports uncertainty until that reconciliation occurs.
        assert (
            connection.execute("SELECT id FROM episodes WHERE id=%s", (episode_id,)).fetchall()
            == []
        )


def test_connection_auth_failure_is_sanitized_before_work_starts(app_env: Any) -> None:
    from potbot.api.wire import error_response

    parsed = urlsplit(app_env.db.dsn)
    bad_dsn = urlunsplit(
        parsed._replace(
            netloc=f"{parsed.username}:incorrect-private-password@{parsed.hostname}:{parsed.port}"
        )
    )
    with pytest.raises(SourceUnavailableError) as failure:
        with Database(bad_dsn).transaction(app_env.context):
            pytest.fail("The login must fail before admitting work")
    response = error_response(failure.value)
    assert response.status == 503
    assert response.body["error"]["code"] == "source_unavailable"
    assert response.body["error"]["retryable"] is False
    assert "incorrect-private-password" not in str(response.body)
    assert parsed.username not in str(response.body)


def test_two_concurrent_command_callers_share_one_durable_result(app_env: Any) -> None:
    barrier = Barrier(2)

    def command() -> dict[str, Any]:
        with app_env.db.transaction(app_env.context) as connection:
            barrier.wait(timeout=10)

            def create() -> dict[str, Any]:
                identity = uuid4()
                connection.execute(
                    "INSERT INTO episodes(workspace_id,id,repository_id,deliverable_type) VALUES (%s,%s,%s,'patch')",
                    (app_env.context.workspace_id, identity, app_env.repository_id),
                )
                return {"episode_id": str(identity)}

            return idempotent_command(
                connection,
                app_env.context,
                "fixture.create",
                "shared",
                {"objective": "same request"},
                create,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: command(), range(2)))
    assert results[0] == results[1]
    with app_env.db.transaction(app_env.context) as connection:
        assert connection.execute("SELECT count(*) AS total FROM episodes").fetchone()["total"] == 1
        assert (
            connection.execute("SELECT count(*) AS total FROM command_results").fetchone()["total"]
            == 1
        )
        with pytest.raises(RevisionConflictError, match="different request"):
            idempotent_command(
                connection,
                app_env.context,
                "fixture.create",
                "shared",
                {"objective": "different"},
                lambda: pytest.fail("Must not execute"),
            )


def test_failed_command_rolls_back_domain_write_and_replay_record(app_env: Any) -> None:
    with pytest.raises(RuntimeError, match="injected"):
        with app_env.db.transaction(app_env.context) as connection:

            def create() -> dict[str, Any]:
                connection.execute(
                    "INSERT INTO episodes(workspace_id,id,repository_id,deliverable_type) VALUES (%s,%s,%s,'patch')",
                    (app_env.context.workspace_id, uuid4(), app_env.repository_id),
                )
                raise RuntimeError("injected between domain data and event")

            idempotent_command(connection, app_env.context, "fixture.create", "failure", {}, create)
    with app_env.db.transaction(app_env.context) as connection:
        assert connection.execute("SELECT count(*) AS total FROM episodes").fetchone()["total"] == 0
        assert (
            connection.execute("SELECT count(*) AS total FROM command_results").fetchone()["total"]
            == 0
        )


def test_append_only_repository_rejects_update_delete_and_truncate(app_env: Any) -> None:
    for statement in (
        "UPDATE repositories SET name='overwrite'",
        "DELETE FROM repositories",
        "TRUNCATE repositories",
    ):
        with pytest.raises(ScopeDeniedError):
            with app_env.db.transaction(app_env.context) as connection:
                connection.execute(statement)
    assert (
        RepositoryStore(app_env.db).get(app_env.context, app_env.repository_id)["name"] == "fixture"
    )


def test_administrative_cli_and_app_repository_registration_share_database_boundary(
    app_env: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from potbot.persistence.admin_cli import main

    monkeypatch.setenv("POTBOT_ADMIN_DSN", app_env.admin_dsn)
    assert main(["migrate"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["versions"] == [
        item.version for item in application_migrations()
    ]
    monkeypatch.setenv("POTBOT_DSN", app_env.db.dsn)
    assert (
        main(
            [
                "repository-create",
                "--workspace-id",
                str(app_env.context.workspace_id),
                "--principal-id",
                str(app_env.context.principal_id),
                "--name",
                "cli-fixture",
                "--source-uri",
                "fixture://cli",
            ]
        )
        == 0
    )
    registered = json.loads(capsys.readouterr().out)
    assert registered["result"]["name"] == "cli-fixture"
    assert (
        main(
            [
                "repository-create",
                "--workspace-id",
                str(app_env.other_context.workspace_id),
                "--principal-id",
                str(app_env.other_context.principal_id),
                "--name",
                "forbidden",
                "--source-uri",
                "fixture://denied",
            ]
        )
        == 1
    )
    denied = json.loads(capsys.readouterr().out)
    assert denied["error"]["code"] == "scope_denied"
    assert "forbidden" not in str(denied)
