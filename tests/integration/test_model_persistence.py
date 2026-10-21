"""Configuration, environment, and invocation provenance on a real SQL service."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from potbot.domain.configuration import resolve_configuration
from potbot.domain.errors import InvalidInputError, OperationTimeoutError, ScopeDeniedError
from potbot.models.configuration import (
    EnvironmentManifest,
    read_environment,
    store_configuration,
    store_environment,
)
from potbot.models.invocations import (
    GenerateRequest,
    GenerationResult,
    InvocationStore,
    PacketSource,
)
from potbot.models.registry import DeploymentSpec, ModelRegistry
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.persistence.database import Database

pytestmark = pytest.mark.postgres


def deployment() -> DeploymentSpec:
    return DeploymentSpec(
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
    )


def test_config_environment_reload_and_immutability(app_env) -> None:
    config = resolve_configuration({"task": {"max_packet_tokens": 400}})
    with app_env.db.transaction(app_env.context) as conn:
        saved = store_configuration(conn, app_env.context, config)
        manifest = EnvironmentManifest(
            saved["id"], config.fingerprint, "linux", "Python-3.12", (("uv", "0.12.23"),)
        )
        environment = store_environment(conn, app_env.context, manifest)
        assert store_environment(conn, app_env.context, manifest)["id"] == environment["id"]
    restarted = Database(app_env.db.dsn, app_env.schema)
    with restarted.transaction(app_env.context) as conn:
        assert (
            read_environment(conn, app_env.context, environment["id"])["configuration_hash"]
            == config.fingerprint
        )
        with pytest.raises(InvalidInputError):
            store_environment(conn, app_env.context, replace(manifest, configuration_hash="f" * 64))
    with pytest.raises(ScopeDeniedError):
        with app_env.db.transaction(app_env.context) as conn:
            conn.execute(
                "UPDATE resolved_configurations SET content='{}' WHERE id=%s", (saved["id"],)
            )
    with app_env.other_db.transaction(app_env.other_context) as conn:
        assert (
            conn.execute(
                "SELECT * FROM environment_manifests WHERE id=%s", (environment["id"],)
            ).fetchone()
            is None
        )


def test_five_unqualified_models_and_health_never_confers_qualification(app_env) -> None:
    registry = ModelRegistry(app_env.db)
    seeded = registry.seed_defaults(app_env.context)
    assert len(seeded) == 5 and all(row["qualification"] == "unqualified" for row in seeded)
    assert registry.seed_defaults(app_env.context) == seeded
    spec = deployment()
    saved = registry.register(app_env.context, spec)
    assert registry.register(app_env.context, spec)["id"] == saved["id"]
    assert (
        registry.register(app_env.context, replace(spec, weights_digest="d" * 64))["id"]
        != saved["id"]
    )
    registry.report_health(app_env.context, saved["id"], "healthy")
    state = registry.inspect(app_env.context, saved["id"])
    assert state["health"] == "healthy" and state["qualified"] is False
    with pytest.raises(ScopeDeniedError):
        with app_env.db.transaction(app_env.context) as conn:
            conn.execute("UPDATE model_deployments SET content='{}' WHERE id=%s", (saved["id"],))


def test_scripted_invocation_captures_exact_inputs_partial_usage_and_scoped_sources(
    app_env,
) -> None:
    registry = ModelRegistry(app_env.db)
    deployed = registry.register(app_env.context, deployment())
    store = ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root))
    content = store.put(app_env.context, b"Requirements and exact source code", "text/plain")
    raw = store.put(app_env.context, b'{"actions":[', "application/json")
    invocations = InvocationStore(app_env.db)
    packet = invocations.create_packet(
        app_env.context,
        deployment_id=deployed["id"],
        artifact_id=content["id"],
        sources=(PacketSource("artifact", content["id"]),),
        token_count=30,
    )
    request = GenerateRequest(
        deployed["id"],
        "reasoning",
        packet["id"],
        "actions-v1",
        '{"temperature":0,"seed":5}',
        100,
        datetime.now(UTC) + timedelta(minutes=1),
        uuid4(),
    )
    assert len(invocations.preflight(app_env.context, request)["input_hash"]) == 64
    with pytest.raises(InvalidInputError):
        invocations.preflight(app_env.context, replace(request, role="coding"))
    with pytest.raises(InvalidInputError):
        invocations.preflight(app_env.context, replace(request, response_schema="unknown-v9"))
    with pytest.raises(OperationTimeoutError):
        invocations.preflight(
            app_env.context, replace(request, deadline=datetime.now(UTC) - timedelta(seconds=1))
        )
    result = GenerationResult(
        raw["id"], None, 30, 5, 12, "cancelled", "partial", "cancelled", "cancelled"
    )
    recorded = invocations.record_final(app_env.context, request, result)
    with Database(app_env.db.dsn).transaction(app_env.context) as conn:
        read = conn.execute(
            "SELECT * FROM model_invocations WHERE id=%s", (recorded["id"],)
        ).fetchone()
        assert read["input_manifest"]["request"]["sampling"] == {"temperature": 0, "seed": 5}
        assert read["raw_output_artifact_id"] == raw["id"] and read["output_tokens"] == 5
        assert read["parse_status"] == "partial"
        sources = conn.execute(
            "SELECT ordinal,artifact_id FROM context_packet_sources WHERE packet_id=%s ORDER BY ordinal",
            (packet["id"],),
        ).fetchall()
        assert sources == [{"ordinal": 0, "artifact_id": content["id"]}]
    with pytest.raises(ScopeDeniedError):
        invocations.create_packet(
            app_env.context,
            deployment_id=deployed["id"],
            artifact_id=content["id"],
            sources=(PacketSource("artifact", uuid4()),),
            token_count=30,
        )
    with pytest.raises(ScopeDeniedError):
        InvocationStore(app_env.other_db).preflight(app_env.other_context, request)
    assert store.read(app_env.context, raw["id"]) == b'{"actions":['
