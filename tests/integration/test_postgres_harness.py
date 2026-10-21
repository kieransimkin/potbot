"""Real PostgreSQL tests. Selecting these tests requires the explicit fixture DSN.

Absence of a database is a failure, never a passing skip. The test-only login
must be able to create disposable databases for the unmarked-target test.
"""

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest

from potbot.persistence.harness import (
    DEFAULT_MIGRATIONS,
    DSN_ENV,
    PGVECTOR_VERSION,
    HarnessError,
    Migration,
    RunHandle,
    TestDatabaseConfig,
    apply_migrations,
    bootstrap,
    check_run,
    cleanup_run,
    create_run,
    list_runs,
    read_manifest,
)

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def postgres_config() -> TestDatabaseConfig:
    try:
        config = TestDatabaseConfig.from_env()
        bootstrap(config)
    except HarnessError as error:
        pytest.fail(f"PostgreSQL integration fixture unavailable: {error}", pytrace=False)
    return config


@pytest.fixture
def run_handle(postgres_config: TestDatabaseConfig) -> Iterator[RunHandle]:
    handle = create_run(postgres_config)
    try:
        yield handle
    finally:
        cleanup_run(postgres_config, handle)


@contextmanager
def driver_connection(config: TestDatabaseConfig) -> Iterator[Any]:
    # The module remains collectable without the optional driver. Once selected,
    # the required fixture above reports missing prerequisites as failures.
    import psycopg

    with psycopg.connect(
        config.dsn,
        hostaddr="::1" if config.host == "::1" else "127.0.0.1",
        options="-c search_path=pg_catalog -c statement_timeout=15000 -c lock_timeout=5000",
        autocommit=True,
        connect_timeout=3,
    ) as connection:
        yield connection


@contextmanager
def unmarked_database(config: TestDatabaseConfig) -> Iterator[TestDatabaseConfig]:
    from psycopg import sql

    database = f"potbot_test_{uuid4().hex}"
    parsed = urlsplit(config.dsn)
    unmarked = TestDatabaseConfig(urlunsplit(parsed._replace(path=f"/{database}")))
    with driver_connection(config) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database))
        )
    try:
        yield unmarked
    finally:
        # The unique name was generated in this scope and created successfully
        # above. This helper never accepts a user-supplied database to drop.
        with driver_connection(config) as connection:
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))


def test_bootstrap_is_idempotent_and_vector_read_uses_application_driver(
    postgres_config: TestDatabaseConfig,
    run_handle: RunHandle,
) -> None:
    assert bootstrap(postgres_config) == run_handle.database_id
    assert apply_migrations(postgres_config, run_handle) == (1,)
    assert apply_migrations(postgres_config, run_handle) == (1,)
    result = check_run(postgres_config, run_handle)
    assert result["postgres_version"] >= 170000
    assert result["pgvector_version"] == PGVECTOR_VERSION
    assert result["vector"] == "[1,2,3]"
    assert result["l2_distance"] == 0.0


def test_fixtures_are_independent_and_cleanup_is_idempotent(
    postgres_config: TestDatabaseConfig,
    run_handle: RunHandle,
) -> None:
    sibling = create_run(postgres_config)
    try:
        assert sibling.schema_name != run_handle.schema_name
        apply_migrations(postgres_config, run_handle)
        apply_migrations(postgres_config, sibling)
        assert cleanup_run(postgres_config, run_handle) is True
        assert cleanup_run(postgres_config, run_handle) is False
        assert check_run(postgres_config, sibling)["l2_distance"] == 0.0
        assert run_handle not in list_runs(postgres_config)
        assert sibling in list_runs(postgres_config)
    finally:
        cleanup_run(postgres_config, sibling)


def test_migration_failure_rolls_back_schema_and_ledger_together(
    postgres_config: TestDatabaseConfig,
    run_handle: RunHandle,
) -> None:
    from psycopg import sql

    apply_migrations(postgres_config, run_handle)
    broken = Migration(
        2,
        "deliberately_broken",
        (
            "CREATE TABLE rollback_probe (value integer)",
            "SELECT 1 / 0",
        ),
    )
    with pytest.raises(HarnessError) as failure:
        apply_migrations(postgres_config, run_handle, (*DEFAULT_MIGRATIONS, broken))
    assert failure.value.code == "database_operation_failed"
    assert "22012" in str(failure.value)
    with driver_connection(postgres_config) as connection:
        assert (
            connection.execute(
                "SELECT to_regclass(%s)", (f"{run_handle.schema_name}.rollback_probe",)
            ).fetchone()[0]
            is None
        )
        ledger = connection.execute(
            sql.SQL("SELECT version FROM {}.schema_migrations ORDER BY version").format(
                sql.Identifier(run_handle.schema_name)
            )
        ).fetchall()
        assert ledger == [(1,)]
    assert check_run(postgres_config, run_handle)["l2_distance"] == 0.0


def test_changed_applied_migration_is_rejected(
    postgres_config: TestDatabaseConfig,
    run_handle: RunHandle,
) -> None:
    apply_migrations(postgres_config, run_handle)
    changed = replace(DEFAULT_MIGRATIONS[0], statements=("SELECT 1",))
    with pytest.raises(HarnessError) as failure:
        apply_migrations(postgres_config, run_handle, (changed,))
    assert failure.value.code == "migration_checksum_mismatch"
    assert check_run(postgres_config, run_handle)["l2_distance"] == 0.0


def test_cleanup_refuses_cross_schema_dependencies_without_partial_drops(
    postgres_config: TestDatabaseConfig,
    run_handle: RunHandle,
) -> None:
    from psycopg import sql

    sibling = create_run(postgres_config)
    view = sql.Identifier(sibling.schema_name, "keep_view")
    apply_migrations(postgres_config, run_handle)
    check_run(postgres_config, run_handle)
    with driver_connection(postgres_config) as connection:
        connection.execute(
            sql.SQL("CREATE VIEW {} AS SELECT label FROM {}.probe_records").format(
                view,
                sql.Identifier(run_handle.schema_name),
            )
        )
    try:
        with pytest.raises(HarnessError) as failure:
            cleanup_run(postgres_config, run_handle)
        assert failure.value.code == "cleanup_scope_conflict"
        assert check_run(postgres_config, run_handle)["l2_distance"] == 0.0
        assert run_handle in list_runs(postgres_config)
        with driver_connection(postgres_config) as connection:
            assert connection.execute(sql.SQL("SELECT label FROM {}").format(view)).fetchone() == (
                "phase-1-driver-check",
            )
    finally:
        with driver_connection(postgres_config) as connection:
            connection.execute(sql.SQL("DROP VIEW {} RESTRICT").format(view))
        cleanup_run(postgres_config, sibling)


def test_cleanup_rejects_wrong_token_and_preserves_the_schema(
    postgres_config: TestDatabaseConfig,
    run_handle: RunHandle,
) -> None:
    apply_migrations(postgres_config, run_handle)
    forged = replace(run_handle, ownership_token="0" * 64)
    with pytest.raises(HarnessError) as failure:
        cleanup_run(postgres_config, forged)
    assert failure.value.code == "run_identity_mismatch"
    assert check_run(postgres_config, run_handle)["l2_distance"] == 0.0


def test_cleanup_rejects_missing_schema_marker(
    postgres_config: TestDatabaseConfig,
    run_handle: RunHandle,
) -> None:
    from psycopg import sql

    marker_table = sql.Identifier(run_handle.schema_name, "_fixture_identity")
    with driver_connection(postgres_config) as connection:
        connection.execute(sql.SQL("DELETE FROM {}").format(marker_table))
    try:
        with pytest.raises(HarnessError) as failure:
            cleanup_run(postgres_config, run_handle)
        assert failure.value.code == "run_identity_mismatch"
        with driver_connection(postgres_config) as connection:
            assert connection.execute(
                "SELECT to_regnamespace(%s)", (run_handle.schema_name,)
            ).fetchone()[0]
    finally:
        with driver_connection(postgres_config) as connection:
            connection.execute(
                sql.SQL(
                    "INSERT INTO {} (database_id, run_id, ownership_token) VALUES (%s, %s, %s)"
                ).format(marker_table),
                (run_handle.database_id, run_handle.run_id, run_handle.ownership_token),
            )


def test_cleanup_rejects_database_without_expected_marker(
    postgres_config: TestDatabaseConfig,
    run_handle: RunHandle,
) -> None:
    with unmarked_database(postgres_config) as unmarked:
        with pytest.raises(HarnessError) as failure:
            cleanup_run(unmarked, run_handle)
        assert failure.value.code == "missing_database_marker"


def test_bootstrap_refuses_nonempty_unmarked_database(postgres_config: TestDatabaseConfig) -> None:
    with unmarked_database(postgres_config) as unmarked:
        with driver_connection(unmarked) as connection:
            connection.execute("CREATE TABLE public.existing_data (value text)")
            connection.execute("INSERT INTO public.existing_data VALUES ('keep-me')")
        with pytest.raises(HarnessError) as failure:
            bootstrap(unmarked)
        assert failure.value.code == "nonempty_unmarked_database"
        with driver_connection(unmarked) as connection:
            assert connection.execute("SELECT value FROM public.existing_data").fetchone() == (
                "keep-me",
            )


@pytest.mark.parametrize("existing_object", ["collation", "large_object"])
def test_bootstrap_refuses_user_objects_outside_table_catalog(
    postgres_config: TestDatabaseConfig,
    existing_object: str,
) -> None:
    with unmarked_database(postgres_config) as unmarked:
        with driver_connection(unmarked) as connection:
            if existing_object == "collation":
                connection.execute('CREATE COLLATION public.keep_me FROM pg_catalog."C"')
                query = "SELECT count(*) FROM pg_catalog.pg_collation WHERE collname = 'keep_me'"
            else:
                connection.execute("SELECT pg_catalog.lo_create(0)")
                query = "SELECT count(*) FROM pg_catalog.pg_largeobject_metadata"
        with pytest.raises(HarnessError) as failure:
            bootstrap(unmarked)
        assert failure.value.code == "nonempty_unmarked_database"
        with driver_connection(unmarked) as connection:
            assert connection.execute(query).fetchone() == (1,)


def test_crashed_worker_leaves_manifest_and_registry_for_restart_cleanup(
    postgres_config: TestDatabaseConfig,
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "interrupted-run.json"
    environment = {**os.environ, DSN_ENV: postgres_config.dsn}
    worker = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, sys\n"
                "from pathlib import Path\n"
                "from potbot.persistence.harness import TestDatabaseConfig, create_run, apply_migrations\n"
                "config = TestDatabaseConfig.from_env()\n"
                "handle = create_run(config, Path(sys.argv[1]))\n"
                "apply_migrations(config, handle)\n"
                "os._exit(17)\n"
            ),
            str(manifest_path),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert worker.returncode == 17, "The worker did not reach the intentional interruption."
    handle = read_manifest(manifest_path)
    try:
        assert handle in list_runs(postgres_config)
        assert check_run(postgres_config, handle)["l2_distance"] == 0.0
        recovered = next(
            item for item in list_runs(postgres_config) if item.run_id == handle.run_id
        )
        assert cleanup_run(postgres_config, recovered) is True
        assert recovered not in list_runs(postgres_config)
    finally:
        cleanup_run(postgres_config, handle)


def test_cli_lifecycle_and_diagnostics_do_not_disclose_connection_secrets(
    postgres_config: TestDatabaseConfig,
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "cli-run.json"
    environment = {**os.environ, DSN_ENV: postgres_config.dsn}

    def invoke(
        *arguments: str, env: dict[str, str] = environment
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "potbot.persistence.harness", *arguments],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    created = invoke("create", "--manifest", str(manifest_path))
    assert created.returncode == 0, created.stderr
    handle = read_manifest(manifest_path)
    try:
        migrated = invoke("migrate", "--manifest", str(manifest_path))
        assert migrated.returncode == 0, migrated.stderr
        checked = invoke("check", "--manifest", str(manifest_path))
        assert checked.returncode == 0, checked.stderr
        assert json.loads(checked.stdout)["l2_distance"] == 0.0
        cleaned = invoke("cleanup", "--manifest", str(manifest_path))
        assert cleaned.returncode == 0, cleaned.stderr
        assert json.loads(cleaned.stdout)["dropped"] is True
        password = urlsplit(postgres_config.dsn).password
        assert password
        assert password not in created.stdout + migrated.stdout + checked.stdout + cleaned.stdout
        missing_environment = {key: value for key, value in environment.items() if key != DSN_ENV}
        failed = invoke("list", env=missing_environment)
        assert failed.returncode != 0
        assert json.loads(failed.stderr)["error"] == "missing_test_dsn"
        assert password not in failed.stderr
    finally:
        cleanup_run(postgres_config, handle)
