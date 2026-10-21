"""Both published migration histories converge without rewriting durable evidence."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from potbot.controller.execution import ExecutionStore
from potbot.controller.tasks import TaskService
from potbot.domain.errors import RevisionConflictError
from potbot.domain.execution import ActionProposal, ToolDefinition, ToolRegistry
from potbot.domain.serialization import content_hash
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement
from potbot.evaluation.repository_fixture import create_repository_fixture
from potbot.models.configuration import EnvironmentManifest, store_environment
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.persistence.database import (
    Database,
    RepositoryStore,
    admin_connection,
    grant_application_role,
    migrate,
    provision_principal,
    provision_workspace,
)
from potbot.persistence.schema import application_migrations
from potbot.tools.repository import RepositoryStateStore

from .conftest import AppEnvironment

pytestmark = pytest.mark.postgres

# Obtained from the original shipped Phase 3 checkout, including the versioned
# migration identity. Reordering or editing its SQL must not change these values.
PHASE3_PREVIEW_CHECKSUMS = (
    "dad7fcb6afd1720b427fb27bc18dfb182f0428112d78696554c7c941d1b04a7d",
    "56ee825cfe2e989dc29c8f520bbca85ba4f314691ef750e2a50712afc300576b",
    "309b25df22966997cd26693cb99e374b9bb9aaf19d5bc86b09d5113e90ea9a47",
    "65bfab18f6bb93d788bd220387780544f93c1b236a11c774d1f6e28602ea8941",
    "5456f844a46117a94a61b50b9e44896890ef25b3a8870d1b29e243aed9bcfc92",
    "fab5e81a276e9b952e081e9262bd0bdf82616bb644219764f9f7e42a406c0ab8",
    "b8fc76d2463d26d803dbaf8206326442fba9e1e521f48828e9ac5700c01b7aae",
    "0bd0651a6f69ec6c0eb73110352c87e38017420e4fe131ae7db25fb642c79223",
)


@pytest.mark.parametrize(("lineage", "prefix"), [("main", 9), ("phase3-preview", 8)])
def test_populated_published_histories_upgrade_with_original_rows_timestamps_and_grants(
    app_env: AppEnvironment,
    tmp_path: Path,
    lineage: str,
    prefix: int,
) -> None:
    from psycopg import sql
    from psycopg.types.json import Jsonb

    revisions = application_migrations(lineage=lineage)
    if lineage == "phase3-preview":
        assert tuple(item.checksum for item in revisions[:8]) == PHASE3_PREVIEW_CHECKSUMS
    schema = "phase4_upgrade"
    assert migrate(app_env.admin_dsn, schema, revisions[:prefix]) == tuple(range(1, prefix + 1))
    workspace = provision_workspace(app_env.admin_dsn, schema, "published-phase3-workspace")
    with app_env.db.transaction(app_env.context) as connection:
        role = connection.execute("SELECT session_user AS role").fetchone()["role"]
    # This also exercises history-aware grants before the upgrade, including the
    # eight-revision preview where revision 6 is repository_schema, not SYN.
    context = provision_principal(app_env.admin_dsn, schema, role, workspace)
    database = Database(app_env.db.dsn, schema)
    repository = RepositoryStore(database).create(
        context, name="existing-repository", source_uri="fixture://phase3"
    )
    spec = ContractSpec(
        "Preserve the published Phase 3 task",
        "fixture-baseline",
        DeliverableType.PATCH,
        (Requirement("R1", "Calculator works", "The repository test suite passes"),),
        ("calculator.py", "tests"),
        ("compile", "test"),
        "fixture-v1",
        (("tokens", 10000), ("calls", 8), ("tool_ms", 300000), ("concurrency", 1)),
    )
    task = TaskService(database).submit(context, repository["id"], spec, "historical-task")
    episode = UUID(task["task_id"])
    contract = UUID(task["current_contract"]["id"])
    artifacts = ArtifactStore(database, LocalArtifactBackend(app_env.artifact_root / "upgrade"))
    with database.transaction(context) as connection:
        environment = store_environment(
            connection,
            context,
            EnvironmentManifest(
                UUID(task["configuration"]["id"]),
                task["configuration"]["content_hash"],
                "linux",
                "Python migration fixture",
                (("git", "fixture"),),
            ),
        )
    fixture = create_repository_fixture(tmp_path / "published-source")
    state = RepositoryStateStore(database, artifacts).capture(
        context, repository["id"], fixture.path, environment["id"]
    )
    definition = ToolDefinition(
        "compile", "compile", ("/usr/bin/python3", "-m", "py_compile", "calculator.py")
    )
    execution = ExecutionStore(database, ToolRegistry((definition,)))
    inputs = execution.create_inputs(context, episode, contract, state["id"], "historical-inputs")
    action_id = uuid4()
    proposal = ActionProposal("compile").to_dict()
    with database.transaction(context) as connection:
        # Current propose() writes Phase 4 stdin_artifact_id. The fixture must
        # insert a genuinely pre-Phase-4 record using the old column contract.
        connection.execute(
            "INSERT INTO actions(workspace_id,id,episode_id,node_id,kind,tool_definition_digest,"
            "proposal,proposal_digest,producer_kind,producer_id) "
            "VALUES (%s,%s,%s,%s,'compile',%s,%s,%s,'controller','phase3-fixture')",
            (
                context.workspace_id,
                action_id,
                episode,
                UUID(inputs["node_id"]),
                definition.fingerprint,
                Jsonb(proposal),
                content_hash(proposal),
            ),
        )
        tables = (
            "episodes",
            "contract_revisions",
            "resolved_configurations",
            "environment_manifests",
            "artifacts",
            "repository_states",
            "semantic_component_releases",
            "semantic_releases",
            "semantic_catalog_entries",
            "semantic_active_releases",
            "task_semantic_pins",
            "knowledge_views",
            "search_nodes",
            "actions",
            "evidence_events",
            "command_results",
        )
        before = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            for table in tables
        }
        assert all(before[table] for table in tables)
    with admin_connection(app_env.admin_dsn, schema) as connection:
        old_ledger = connection.execute(
            "SELECT * FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert migrate(app_env.admin_dsn, schema) == tuple(item.version for item in revisions)
    with admin_connection(app_env.admin_dsn, schema) as connection:
        ledger = connection.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
        assert ledger[:prefix] == old_ledger  # Includes every applied_at timestamp.
        assert tuple(row["checksum"] for row in ledger) == tuple(
            item.checksum for item in revisions
        )
        assert ledger[5]["name"] == ("syn_schema" if lineage == "main" else "repository_schema")
        assert ledger[8]["name"] == ("execution_schema" if lineage == "main" else "syn_schema")
        # Regrant is independently required to recognize both published histories.
        connection.execute(
            sql.SQL("REVOKE ALL ON implementation_contracts FROM {}").format(sql.Identifier(role))
        )
    grant_application_role(app_env.admin_dsn, schema, role)
    with database.transaction(context) as connection:
        for table, rows in before.items():
            upgraded = connection.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            if table == "actions":
                assert all(row["stdin_artifact_id"] is None for row in upgraded)
                upgraded = [
                    {key: value for key, value in row.items() if key != "stdin_artifact_id"}
                    for row in upgraded
                ]
            assert upgraded == rows, table
        assert connection.execute("SELECT * FROM implementation_contracts").fetchall() == []
        connection.execute(
            "INSERT INTO implementation_contracts(workspace_id,id,episode_id,contract_revision_id,"
            "base_state_id,content,content_digest) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                workspace,
                uuid4(),
                episode,
                contract,
                state["id"],
                Jsonb({"upgrade": True}),
                content_hash({"upgrade": True}),
            ),
        )
        connection.execute(
            "INSERT INTO syn_namespaces(workspace_id,id,name,repository_id) VALUES (%s,%s,%s,%s)",
            (workspace, uuid4(), "upgraded-syn-grants", repository["id"]),
        )
    assert execution.inspect(context, action_id)["action"]["proposal"] == proposal
    assert migrate(app_env.admin_dsn, schema) == tuple(item.version for item in revisions)
    with admin_connection(app_env.admin_dsn, schema) as connection:
        assert (
            connection.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
            == ledger
        )


def test_legacy_lineage_detection_still_rejects_modified_historical_checksums(
    app_env: AppEnvironment,
) -> None:
    schema = "phase4_damaged_history"
    revisions = application_migrations(lineage="phase3-preview")
    migrate(app_env.admin_dsn, schema, revisions[:8])
    with app_env.db.transaction(app_env.context) as connection:
        role = connection.execute("SELECT session_user AS role").fetchone()["role"]
    with admin_connection(app_env.admin_dsn, schema) as connection:
        connection.execute("UPDATE schema_migrations SET checksum=%s WHERE version=6", ("0" * 64,))
        damaged = connection.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
    with pytest.raises(RevisionConflictError):
        migrate(app_env.admin_dsn, schema)
    with pytest.raises(RevisionConflictError):
        grant_application_role(app_env.admin_dsn, schema, role)
    with admin_connection(app_env.admin_dsn, schema) as connection:
        assert (
            connection.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
            == damaged
        )
        assert (
            connection.execute("SELECT to_regclass('implementation_contracts') AS name").fetchone()[
                "name"
            ]
            is None
        )
