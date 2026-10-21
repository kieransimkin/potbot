"""Real SQL release selection and frozen historical task/context identities."""

import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from potbot.controller.information_needs import InformationNeedStore, NeedSpec
from potbot.controller.tasks import TaskService
from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.tasks import ContractSpec
from potbot.models.invocations import InvocationStore, PacketSource
from potbot.models.registry import DeploymentSpec, ModelRegistry
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.persistence.database import Database, admin_connection
from potbot.semantics.registry import CONSUMER_CONTRACT, REGISTRY_DOCUMENTS, SemanticRegistry
from potbot.semantics.releases import SemanticReleaseStore, read_task_pin

pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[2]


def contract() -> ContractSpec:
    return ContractSpec.from_dict(json.loads((ROOT / "examples/task-contract.json").read_text()))


def next_registry(*, rename_profile: bool = False) -> SemanticRegistry:
    return SemanticRegistry.from_documents(
        {
            name: (
                (ROOT / "vocab" / name).read_bytes()
                if name == "artifact-embedding-manifest.schema.json"
                else (ROOT / "vocab" / name)
                .read_bytes()
                .replace(b"0.1.0", b"0.1.1")
                .replace(b"aboutness", b"subject_relevance" if rename_profile else b"aboutness")
            )
            for name in REGISTRY_DOCUMENTS
        }
    )


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


def test_new_tasks_and_packets_freeze_selected_release_without_rewriting_history(app_env):
    service = TaskService(app_env.db)
    first = service.submit(app_env.context, app_env.repository_id, contract(), "release-a")
    first_pin = first["semantic_configuration"]
    first_config = first["configuration"]
    store = SemanticReleaseStore(app_env.db)
    registry_b = next_registry()
    with pytest.raises(CapabilityUnavailableError):
        store.register(app_env.context, registry_b)
    release_b = store.register(app_env.context, registry_b, consumer_contract=CONSUMER_CONTRACT)
    selected = store.select(app_env.context, release_b["id"], expected_token=0)
    assert selected["update_token"] == 1
    second = service.submit(app_env.context, app_env.repository_id, contract(), "release-b")
    assert second["semantic_configuration"]["release_id"] == str(release_b["id"])
    assert first_pin["release_id"] != second["semantic_configuration"]["release_id"]
    artifacts = ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root))
    text = artifacts.put(app_env.context, b"packet fixture", "text/plain")
    model = ModelRegistry(app_env.db).register(app_env.context, deployment())
    packets = InvocationStore(app_env.db)
    records = []
    for task in (first, second):
        packet = packets.create_packet(
            app_env.context,
            deployment_id=model["id"],
            artifact_id=text["id"],
            sources=(PacketSource("artifact", text["id"]),),
            token_count=8,
            episode_id=UUID(task["task_id"]),
        )
        assert packet["content"]["schema_version"] == 2
        assert packet["content"]["semantic_release"] == task["semantic_configuration"]["content"]
        records.append(packet)
    restarted = TaskService(Database(app_env.db.dsn))
    recovered = restarted.inspect(app_env.context, UUID(first["task_id"]))
    assert recovered["configuration"] == first_config
    assert recovered["semantic_configuration"] == first_pin
    assert service.submit(app_env.context, app_env.repository_id, contract(), "release-a") == first
    with app_env.db.transaction(app_env.context) as conn:
        for packet in records:
            persisted = conn.execute(
                "SELECT content_hash,content FROM context_packets WHERE id=%s", (packet["id"],)
            ).fetchone()
            assert persisted == {key: packet[key] for key in ("content_hash", "content")}
    with pytest.raises(RevisionConflictError):
        store.select(app_env.context, release_b["id"], expected_token=0)


def test_immutable_component_releases_scope_and_compatibility(app_env):
    store = SemanticReleaseStore(app_env.db)
    original = store.register(app_env.context)
    assert store.register(app_env.context)["id"] == original["id"]
    assert len(store.inventory(app_env.context, original["id"], "data_type")) == 45
    documents = {name: (ROOT / "vocab" / name).read_bytes() for name in REGISTRY_DOCUMENTS}
    vocabulary = json.loads(documents["vocabulary.json"])
    vocabulary["entity_types"][0]["definition"] = (
        "A changed meaning under the same published release"
    )
    documents["vocabulary.json"] = json.dumps(vocabulary).encode()
    with pytest.raises(RevisionConflictError):
        store.register(
            app_env.context,
            SemanticRegistry.from_documents(documents),
            consumer_contract=CONSUMER_CONTRACT,
        )
    with pytest.raises(CapabilityUnavailableError):
        store.register(app_env.context, consumer_contract="unsupported-v999")
    with pytest.raises(ScopeDeniedError):
        SemanticReleaseStore(app_env.other_db).inspect(app_env.other_context, original["id"])
    with pytest.raises(ScopeDeniedError):
        store.select(app_env.context, uuid4(), expected_token=None)
    with pytest.raises(ScopeDeniedError):
        with app_env.db.transaction(app_env.context) as conn:
            conn.execute(
                "UPDATE semantic_releases SET consumer_contract='evil' WHERE id=%s",
                (original["id"],),
            )
    assert store.inspect(app_env.context, original["id"])["consumer_contract"] == CONSUMER_CONTRACT


def test_exact_catalog_inventory_detects_a_missing_registered_type(app_env):
    store = SemanticReleaseStore(app_env.db)
    release = store.register(app_env.context)
    # Deliberate owner-only corruption in this disposable fixture. Ordinary application
    # roles have neither DELETE permission nor permission to disable immutable triggers.
    with admin_connection(app_env.admin_dsn) as conn:
        conn.execute("ALTER TABLE semantic_catalog_entries DISABLE TRIGGER immutable_record")
        conn.execute(
            "DELETE FROM semantic_catalog_entries WHERE workspace_id=%s AND release_id=%s AND kind='data_type' AND entry_id='dt:agent.run'",
            (app_env.context.workspace_id, release["id"]),
        )
        conn.execute("ALTER TABLE semantic_catalog_entries ENABLE TRIGGER immutable_record")
    with pytest.raises(StateMismatchError, match="incomplete"):
        store.inventory(app_env.context, release["id"], "data_type")


def test_task_pin_reference_ownership_and_legacy_are_explicit(app_env):
    service = TaskService(app_env.db)
    first = service.submit(app_env.context, app_env.repository_id, contract(), "one")
    second = service.submit(app_env.context, app_env.repository_id, contract(), "two")
    with pytest.raises(ScopeDeniedError):
        with app_env.db.transaction(app_env.context) as conn:
            # Composite SQL FK checks task/config agreement, even below the service.
            conn.execute(
                "INSERT INTO task_semantic_pins(workspace_id,episode_id,release_id,configuration_id,content_hash,content) "
                "VALUES(%s,%s,%s,%s,%s,'{}')",
                (
                    app_env.context.workspace_id,
                    uuid4(),
                    UUID(first["semantic_configuration"]["release_id"]),
                    UUID(second["configuration"]["id"]),
                    "f" * 64,
                ),
            )
    with app_env.db.transaction(app_env.context) as conn:
        with pytest.raises(CapabilityUnavailableError):
            read_task_pin(conn, app_env.context, uuid4())
        assert read_task_pin(conn, app_env.context, uuid4(), required=False) is None


def test_information_needs_resolve_profiles_against_their_frozen_task_release(app_env):
    service = TaskService(app_env.db)
    task_a = service.submit(app_env.context, app_env.repository_id, contract(), "need-release-a")
    needs = InformationNeedStore(app_env.db)
    spec_a = NeedSpec(
        question="Which existing library or workflow implements the requested behavior?",
        pending_decision="Reuse an existing implementation or develop a new one",
        alternatives=("Reuse", "Adapt", "Implement"),
        required_evidence=("Versioned interface and an executable example",),
        resolution_criteria=("The example satisfies the task's requirements",),
        scope="Authorized repository and workflow sources",
        query_profiles=("aboutness", "applicable_procedure"),
    )
    first = needs.create(app_env.context, UUID(task_a["task_id"]), spec_a, "initial-need-a")

    releases = SemanticReleaseStore(app_env.db)
    release_b = releases.register(
        app_env.context, next_registry(rename_profile=True), consumer_contract=CONSUMER_CONTRACT
    )
    releases.select(app_env.context, release_b["id"], expected_token=0)
    task_b = service.submit(app_env.context, app_env.repository_id, contract(), "need-release-b")
    spec_b = replace(spec_a, query_profiles=("subject_relevance", "applicable_procedure"))
    second = needs.create(app_env.context, UUID(task_b["task_id"]), spec_b, "initial-need-b")

    for record, task, names, version in (
        (first, task_a, spec_a.query_profiles, "0.1.0"),
        (second, task_b, spec_b.query_profiles, "0.1.1"),
    ):
        assert (
            record["content"]["semantic_release_id"] == task["semantic_configuration"]["release_id"]
        )
        assert [value["id"] for value in record["content"]["resolved_query_profiles"]] == [
            f"urn:agent-sem:retrieval-profile:{name}:{version}" for name in names
        ]

    # Updating an A task while B is active keeps its original profile definitions.
    revised = needs.revise(
        app_env.context,
        UUID(first["id"]),
        0,
        UUID(first["current_revision_id"]),
        replace(spec_a, question="Which existing workflow supports the revised requirement?"),
        "revise-need-a",
    )
    assert (
        revised["content"]["resolved_query_profiles"] == first["content"]["resolved_query_profiles"]
    )
    assert revised["content"]["semantic_release_id"] == first["content"]["semantic_release_id"]
    # A fresh need for the historical task also resolves A instead of current B.
    later_a = needs.create(app_env.context, UUID(task_a["task_id"]), spec_a, "later-need-a")
    assert (
        later_a["content"]["resolved_query_profiles"] == first["content"]["resolved_query_profiles"]
    )

    with pytest.raises(InvalidInputError, match="pinned release"):
        needs.create(app_env.context, UUID(task_a["task_id"]), spec_b, "wrong-profile-a")
    with pytest.raises(InvalidInputError, match="pinned release"):
        needs.create(app_env.context, UUID(task_b["task_id"]), spec_a, "wrong-profile-b")
    with pytest.raises(InvalidInputError, match="pinned release"):
        needs.revise(
            app_env.context,
            UUID(first["id"]),
            1,
            UUID(revised["current_revision_id"]),
            spec_b,
            "wrong-revision-profile",
        )
    restarted = InformationNeedStore(Database(app_env.db.dsn))
    assert restarted.inspect(app_env.context, UUID(first["id"])) == revised
    history = restarted.history(app_env.context, UUID(first["id"]))
    assert len(history) == 2
    assert history[0]["content"] == first["content"]
    assert needs.create(app_env.context, UUID(task_a["task_id"]), spec_a, "initial-need-a") == first
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM information_needs").fetchone()["n"] == 3
        assert conn.execute("SELECT count(*) AS n FROM need_revisions").fetchone()["n"] == 4
