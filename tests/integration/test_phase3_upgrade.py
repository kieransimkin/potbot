"""Populated Phase 2 rollforward and the real operator execution entry point."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from potbot.cli import main
from potbot.controller.budgets import BudgetLedger
from potbot.controller.execution import ExecutionStore
from potbot.controller.tasks import TaskService
from potbot.domain.configuration import resolve_configuration
from potbot.domain.errors import CapabilityUnavailableError
from potbot.domain.execution import ActionProposal, CapabilityContext, ToolDefinition, ToolRegistry
from potbot.domain.serialization import content_hash
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus
from potbot.evaluation.repository_fixture import (
    create_repository_fixture,
    repair_repository_fixture,
)
from potbot.memory.embedding_profile import EmbeddingProfile
from potbot.models.configuration import EnvironmentManifest, store_configuration, store_environment
from potbot.models.invocations import GenerateRequest, InvocationStore, PacketSource
from potbot.models.registry import DeploymentSpec, ModelRegistry
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.persistence.database import (
    Database,
    RepositoryStore,
    admin_connection,
    migrate,
    provision_principal,
    provision_workspace,
)
from potbot.persistence.evidence import append_event
from potbot.persistence.schema import application_migrations
from potbot.tools.gateway import ToolGateway
from potbot.tools.receipts import RunReceiptStore
from potbot.tools.repository import CandidateWorkspaceManager, RepositoryStateStore

pytestmark = pytest.mark.postgres

# Shipped through ff5510e. New migrations must accept this exact historical
# ledger, including the named-vector migration, without rewriting its checksums.
PHASE2_CHECKSUMS = (
    "dad7fcb6afd1720b427fb27bc18dfb182f0428112d78696554c7c941d1b04a7d",
    "56ee825cfe2e989dc29c8f520bbca85ba4f314691ef750e2a50712afc300576b",
    "309b25df22966997cd26693cb99e374b9bb9aaf19d5bc86b09d5113e90ea9a47",
    "65bfab18f6bb93d788bd220387780544f93c1b236a11c774d1f6e28602ea8941",
    "5456f844a46117a94a61b50b9e44896890ef25b3a8870d1b29e243aed9bcfc92",
)


def specification() -> ContractSpec:
    return ContractSpec(
        "Verify the calculator patch",
        "fixture-baseline",
        DeliverableType.PATCH,
        (Requirement("R1", "Calculator works", "The repository test suite passes"),),
        ("calculator.py", "tests"),
        ("compile", "test"),
        "fixture-v1",
        (("tokens", 10000), ("calls", 8), ("tool_ms", 300000), ("concurrency", 1)),
    )


def seed_phase2(app_env):
    """Use v5-compatible SQL; current task submission requires semantic releases."""
    from psycopg.types.json import Jsonb

    schema = "phase3_upgrade"
    assert migrate(app_env.admin_dsn, schema, application_migrations()[:5]) == (1, 2, 3, 4, 5)
    workspace = provision_workspace(app_env.admin_dsn, schema, "existing-phase2-workspace")
    with app_env.db.transaction(app_env.context) as connection:
        role = connection.execute("SELECT session_user AS role").fetchone()["role"]
    context = provision_principal(app_env.admin_dsn, schema, role, workspace)
    database = Database(app_env.db.dsn, schema)
    repository = RepositoryStore(database).create(
        context, name="existing-repository", source_uri="fixture://phase2"
    )
    artifacts = ArtifactStore(database, LocalArtifactBackend(app_env.artifact_root / "upgrade"))
    artifact = artifacts.put(context, b"Reuse a verified calculator workflow.", "text/plain")
    deployment = ModelRegistry(database).register(
        context,
        DeploymentSpec(
            "microsoft/Phi-4-mini-instruct",
            "a" * 64,
            "scripted",
            "v1",
            "none",
            "b" * 40,
            "c" * 64,
            "json-v1",
            "none",
            2048,
            256,
            (("reason", "v1"),),
            ("reasoning",),
            ("actions-v1",),
        ),
    )
    episode_id, contract_id, packet_id, source_id, space_id, reservation_id = (
        uuid4() for _ in range(6)
    )
    contract = specification().to_dict()
    configuration = resolve_configuration({"task": {"max_packet_tokens": 1536}})
    payload = {
        "schema_version": 1,
        "deployment_id": str(deployment["id"]),
        "artifact_id": str(artifact["id"]),
        "sources": [{"kind": "artifact", "id": str(artifact["id"])}],
        "token_count": 6,
        "policy_version": "packet-v1",
    }
    profile = EmbeddingProfile()
    text = "Reuse a verified calculator workflow."
    metadata = {"revision": "phase2-v1", "kind": "workflow"}
    source_hash = content_hash({"text": text, "metadata": metadata})
    with database.transaction(context) as connection:
        connection.execute(
            "INSERT INTO episodes(workspace_id,id,repository_id,deliverable_type,status,"
            "previous_status,update_token) VALUES(%s,%s,%s,'patch','paused','running',2)",
            (workspace, episode_id, repository["id"]),
        )
        connection.execute(
            "INSERT INTO contract_revisions(workspace_id,id,episode_id,content_hash,content) "
            "VALUES(%s,%s,%s,%s,%s)",
            (workspace, contract_id, episode_id, content_hash(contract), Jsonb(contract)),
        )
        connection.execute(
            "UPDATE episodes SET active_contract_id=%s WHERE id=%s", (contract_id, episode_id)
        )
        connection.execute(
            "INSERT INTO budget_limits(workspace_id,id,dimensions) VALUES(%s,%s,%s)",
            (workspace, episode_id, Jsonb(dict(specification().budget))),
        )
        config = store_configuration(connection, context, configuration, episode_id=episode_id)
        connection.execute(
            "INSERT INTO context_packets(workspace_id,id,episode_id,deployment_id,artifact_id,"
            "content_hash,content,token_count) VALUES(%s,%s,%s,%s,%s,%s,%s,6)",
            (
                workspace,
                packet_id,
                episode_id,
                deployment["id"],
                artifact["id"],
                content_hash(payload),
                Jsonb(payload),
            ),
        )
        connection.execute(
            "INSERT INTO context_packet_sources(workspace_id,id,packet_id,ordinal,artifact_id) "
            "VALUES(%s,%s,%s,0,%s)",
            (workspace, uuid4(), packet_id, artifact["id"]),
        )
        append_event(
            connection,
            context,
            "controller_decision",
            episode_id=episode_id,
            payload_id=artifact["id"],
            details={"decision": "Check an existing workflow first"},
        )
        connection.execute(
            "INSERT INTO embedding_spaces(workspace_id,id,content_hash,content,dimensions,metric) "
            "VALUES(%s,%s,%s,%s,1024,'cosine')",
            (workspace, space_id, profile.fingerprint, Jsonb(profile.to_dict())),
        )
        connection.execute(
            "INSERT INTO embedding_views(workspace_id,id,embedding_name,space_id) "
            "VALUES(%s,%s,'workflow_text',%s)",
            (workspace, uuid4(), space_id),
        )
        connection.execute(
            "INSERT INTO embedding_sources(workspace_id,id,repository_id,source_key,artifact_id,"
            "content_hash,content,metadata) VALUES(%s,%s,%s,'existing-workflow',%s,%s,%s,%s)",
            (
                workspace,
                source_id,
                repository["id"],
                artifact["id"],
                source_hash,
                text,
                Jsonb(metadata),
            ),
        )
        connection.execute(
            "INSERT INTO embedding_source_heads(workspace_id,id,repository_id,source_key,source_id,"
            "generation,active) VALUES(%s,%s,%s,'existing-workflow',%s,1,true)",
            (workspace, uuid4(), repository["id"], source_id),
        )
        usage = {"tokens": 6, "calls": 1}
        connection.execute(
            "INSERT INTO budget_reservations(workspace_id,id,episode_id,reservation_key,amounts,status,actual,origin) "
            "VALUES(%s,%s,%s,'existing-encoding',%s,'settled',%s,'synthetic')",
            (workspace, reservation_id, episode_id, Jsonb(usage), Jsonb(usage)),
        )
        connection.execute(
            "INSERT INTO budget_usage(workspace_id,id,episode_id,reservation_id,amounts,origin) "
            "VALUES(%s,%s,%s,%s,%s,'synthetic')",
            (workspace, uuid4(), episode_id, reservation_id, Jsonb(usage)),
        )
        connection.execute(
            "INSERT INTO embedding_vectors(workspace_id,id,source_id,space_id,embedding,content_hash,"
            "reservation_id,usage_origin,embedding_name) VALUES(%s,%s,%s,%s,%s::public.vector,%s,%s,'synthetic','workflow_text')",
            (
                workspace,
                uuid4(),
                source_id,
                space_id,
                json.dumps([1.0] + [0.0] * 1023),
                source_hash,
                reservation_id,
            ),
        )
        historical_tables = (
            "episodes",
            "contract_revisions",
            "resolved_configurations",
            "context_packets",
            "context_packet_sources",
            "model_deployments",
            "artifacts",
            "evidence_events",
            "embedding_spaces",
            "embedding_views",
            "embedding_sources",
            "embedding_source_heads",
            "embedding_vectors",
            "budget_reservations",
            "budget_usage",
        )
        before = {
            name: connection.execute(f"SELECT * FROM {name} ORDER BY id").fetchall()
            for name in historical_tables
        }
    return SimpleNamespace(
        schema=schema,
        db=database,
        context=context,
        repository=repository,
        artifacts=artifacts,
        artifact=artifact,
        deployment=deployment,
        episode_id=episode_id,
        contract_id=contract_id,
        packet_id=packet_id,
        config=config,
        packet=payload,
        before=before,
    )


def test_populated_v5_rollforward_preserves_historical_hashes_and_enables_new_task_execution(
    app_env, tmp_path: Path
) -> None:
    revisions = application_migrations()
    assert tuple(item.checksum for item in revisions[:5]) == PHASE2_CHECKSUMS
    assert revisions[4].name == "named_vector_schema"
    old = seed_phase2(app_env)
    with admin_connection(app_env.admin_dsn, old.schema) as connection:
        old_ledger = connection.execute(
            "SELECT * FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert migrate(app_env.admin_dsn, old.schema) == tuple(item.version for item in revisions)
    with admin_connection(app_env.admin_dsn, old.schema) as connection:
        new_ledger = connection.execute(
            "SELECT * FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert new_ledger[:5] == old_ledger
        assert tuple(row["checksum"] for row in new_ledger) == tuple(
            item.checksum for item in revisions
        )
    with old.db.transaction(old.context) as connection:
        for name, rows in old.before.items():
            upgraded = connection.execute(f"SELECT * FROM {name} ORDER BY id").fetchall()
            if name == "evidence_events":
                assert all(row["run_id"] is None and row["node_id"] is None for row in upgraded)
                upgraded = [
                    {key: value for key, value in row.items() if key not in {"run_id", "node_id"}}
                    for row in upgraded
                ]
            assert upgraded == rows, name
        assert connection.execute("SELECT * FROM task_semantic_pins").fetchall() == []
        assert connection.execute("SELECT * FROM packet_semantic_pins").fetchall() == []
    tasks = TaskService(old.db)
    historical = tasks.inspect(old.context, old.episode_id)
    assert historical["configuration"]["content_hash"] == old.config["content_hash"]
    assert historical["semantic_configuration"] is None
    resumed = tasks.control(
        old.context,
        old.episode_id,
        2,
        "resume",
        "Resume the legacy lifecycle",
        idempotency_key="legacy-resume",
    )
    assert resumed["lifecycle"] == "running" and resumed["semantic_configuration"] is None
    request = GenerateRequest(
        old.deployment["id"],
        "reasoning",
        old.packet_id,
        "actions-v1",
        '{"temperature":0}',
        64,
        datetime.now(UTC) + timedelta(minutes=1),
        uuid4(),
    )
    manifest = InvocationStore(old.db).preflight(old.context, request)["input_manifest"]
    assert manifest["context_packet_hash"] == content_hash(old.packet)
    with pytest.raises(CapabilityUnavailableError):
        InvocationStore(old.db).create_packet(
            old.context,
            deployment_id=old.deployment["id"],
            artifact_id=old.artifact["id"],
            sources=(PacketSource("artifact", old.artifact["id"]),),
            token_count=6,
            episode_id=old.episode_id,
        )

    new = tasks.submit(old.context, old.repository["id"], specification(), "new-phase3-task")
    episode = UUID(new["task_id"])
    assert new["semantic_configuration"]["configuration_id"] == new["configuration"]["id"]
    for status in (TaskStatus.INITIALIZING, TaskStatus.RUNNING):
        new = tasks.transition(
            old.context,
            episode,
            new["update_token"],
            status,
            "Upgrade execution fixture ready",
            idempotency_key=status.value,
        )
    with old.db.transaction(old.context) as connection:
        environment = store_environment(
            connection,
            old.context,
            EnvironmentManifest(
                UUID(new["configuration"]["id"]),
                new["configuration"]["content_hash"],
                "linux",
                "Python upgrade fixture",
                (("git", "fixture"),),
            ),
        )
    fixture = create_repository_fixture(tmp_path / "upgrade-source")
    state = RepositoryStateStore(old.db, old.artifacts).capture(
        old.context, old.repository["id"], fixture.path, environment["id"]
    )
    manager = CandidateWorkspaceManager(old.db, old.artifacts, tmp_path / "upgrade-candidates")
    manager.allocate(old.context, state["id"])
    registry = ToolRegistry(
        (
            ToolDefinition(
                "compile", "compile", ("/usr/bin/python3", "-m", "py_compile", "calculator.py")
            ),
        )
    )
    execution = ExecutionStore(old.db, registry)
    with pytest.raises(CapabilityUnavailableError):
        execution.create_inputs(
            old.context, old.episode_id, old.contract_id, state["id"], "legacy-execution"
        )
    inputs = execution.create_inputs(
        old.context, episode, UUID(new["current_contract"]["id"]), state["id"], "new-inputs"
    )
    action = execution.propose(
        old.context, UUID(inputs["node_id"]), ActionProposal("compile"), "new-action"
    )
    assert execution.inspect(old.context, UUID(action["id"]))["run"] is None
    gateway = ToolGateway(
        old.db,
        old.artifacts,
        manager,
        registry,
        CapabilityContext(registry.inventory()),
        RunReceiptStore(tmp_path / "upgrade-receipts"),
    )
    assert gateway.resume(old.context, old.episode_id)["may_schedule"] is False
    assert gateway.resume(old.context, episode)["may_schedule"] is True
    packet = InvocationStore(old.db).create_packet(
        old.context,
        deployment_id=old.deployment["id"],
        artifact_id=old.artifact["id"],
        sources=(PacketSource("artifact", old.artifact["id"]),),
        token_count=6,
        episode_id=episode,
    )
    assert packet["content"]["schema_version"] == 2
    assert packet["content"]["semantic_release"] == new["semantic_configuration"]["content"]
    assert tasks.inspect(old.context, old.episode_id)["semantic_configuration"] is None
    assert migrate(app_env.admin_dsn, old.schema) == tuple(item.version for item in revisions)
    with admin_connection(app_env.admin_dsn, old.schema) as connection:
        assert (
            connection.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
            == new_ledger
        )
    with old.db.transaction(old.context) as connection:
        assert (
            connection.execute(
                "SELECT * FROM resolved_configurations WHERE id=%s", (old.config["id"],)
            ).fetchone()
            == old.before["resolved_configurations"][0]
        )
        assert (
            connection.execute(
                "SELECT * FROM context_packets WHERE id=%s", (old.packet_id,)
            ).fetchone()
            == old.before["context_packets"][0]
        )
        assert (
            connection.execute("SELECT * FROM embedding_vectors").fetchall()
            == old.before["embedding_vectors"]
        )


@pytest.mark.sandbox
def test_cli_prepare_compile_test_and_same_key_replay_use_real_durable_execution(
    app_env, tmp_path: Path, monkeypatch, capsys
) -> None:
    from potbot.workspace_sandbox import load_workspace_sandbox

    sandbox = load_workspace_sandbox(app_env.db, app_env.context)
    fixture = create_repository_fixture(sandbox.path("repository") / "cli-source")
    repair_repository_fixture(fixture)
    test_file = fixture.path / "tests" / "test_calculator.py"
    test_file.write_text(
        "from pathlib import Path\n"
        "counter = Path('cli-executions.txt')\n"
        "counter.write_text(str((int(counter.read_text()) if counter.exists() else 0) + 1))\n"
        + test_file.read_text()
    )
    task = TaskService(app_env.db).submit(
        app_env.context, app_env.repository_id, specification(), "cli-task"
    )
    monkeypatch.setenv("POTBOT_DSN", app_env.db.dsn)
    arguments = [
        "execution",
        "--dev-auth",
        "--workspace",
        str(app_env.context.workspace_id),
        "--principal",
        str(app_env.context.principal_id),
        "--artifacts",
        str(app_env.artifact_root),
        "--candidates",
        str(sandbox.path("candidates") / "cli-candidates"),
        "--receipts",
        str(sandbox.path("receipts") / "cli-receipts"),
    ]

    def invoke(*command):
        status = main([*arguments, *command])
        output = capsys.readouterr()
        assert output.err == ""
        response = json.loads(output.out)
        assert status == 0, response
        assert response["schema_version"] == 1
        return response["data"]

    prepared = invoke("prepare", task["task_id"], "--source", str(fixture.path))
    assert prepared["lifecycle"] == "running"
    candidate = prepared["candidate"]
    compiled = invoke(
        "run",
        task["task_id"],
        "--candidate",
        candidate["id"],
        "--tool",
        "compile.python",
        "--key",
        "cli-compile",
    )
    assert compiled["completion"]["outcome"] == "completed"
    test_arguments = (
        "run",
        task["task_id"],
        "--candidate",
        candidate["id"],
        "--tool",
        "test.python",
        "--key",
        "cli-test",
    )
    tested = invoke(*test_arguments)
    assert tested["completion"]["outcome"] == "completed"
    assert (Path(candidate["path"]) / "cli-executions.txt").read_text() == "1"
    assert invoke(*test_arguments) == tested
    assert invoke("inspect", tested["action"]["id"]) == tested
    assert (Path(candidate["path"]) / "cli-executions.txt").read_text() == "1"
    assert not (fixture.path / "cli-executions.txt").exists()
    artifacts = ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root))
    stderr = artifacts.read(app_env.context, UUID(tested["completion"]["stderr_artifact_id"]))
    assert b"Ran 2 tests" in stderr and b"OK" in stderr
    with app_env.db.transaction(app_env.context) as connection:
        assert connection.execute("SELECT count(*) AS n FROM action_runs").fetchone()["n"] == 2
        assert (
            connection.execute("SELECT count(*) AS n FROM budget_reservations").fetchone()["n"] == 2
        )
        assert (
            connection.execute(
                "SELECT status FROM workspace_leases WHERE candidate_id=%s",
                (UUID(candidate["id"]),),
            ).fetchone()["status"]
            == "released"
        )
    budget = BudgetLedger(app_env.db).inspect(app_env.context, UUID(task["task_id"]))
    assert budget["used"]["calls"] == 2 and not budget["held"]
