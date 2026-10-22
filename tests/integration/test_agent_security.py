"""Real PostgreSQL publication, exact inventory and owner contract integration."""

from typing import Any
from uuid import UUID, uuid4

import pytest

from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.security.public import public_identity_descriptor
from potbot.semantics.agent_security import build_agent_security_view, locking_cog_configuration
from potbot.semantics.ember_service import EmberContractService
from potbot.semantics.service import SynService

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("group", [False, True])
def test_typed_agent_security_import_and_owner_resolution(app_env: Any, group: bool) -> None:
    identity = public_identity_descriptor(bytes(range(32)))
    policy = {
        "workspace_id": str(app_env.context.workspace_id),
        "allowed_audiences": ["potbot-local"],
        "allowed_payload_types": ["syn://type/ember"],
        "allowed_domains": ["potbot.event.v2"],
    }
    view = build_agent_security_view(
        identity,
        expected_signer_hash=str(identity["signer_hash"]),
        workspace_id=app_env.context.workspace_id,
        namespace="agent-security",
        snapshot_id=uuid4(),
        policy=policy,
        group=group,
    )
    artifacts = ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root))
    service = SynService(app_env.db, artifacts)
    imported = service.import_snapshot(
        app_env.context, app_env.repository_id, "agent-security", view.snapshot
    )
    snapshot_id = UUID(imported["scope"]["snapshot_id"])
    contracts = EmberContractService(app_env.db)
    stored = contracts.import_bundle(
        app_env.context, snapshot_id, view.with_snapshot_id(snapshot_id)
    )
    assert (
        contracts.import_bundle(app_env.context, snapshot_id, view.with_snapshot_id(snapshot_id))
        == stored
    )
    bundle_id = UUID(stored["bundle_id"])
    owner, types = contracts.read_bundle(app_env.context, bundle_id, projection="owner")
    assert locking_cog_configuration(owner, types, view.locking_cog_ref) == {
        "signer_hash": view.signer_hash,
        "policy": policy,
    }
    public, _ = contracts.read_bundle(app_env.context, bundle_id)
    assert public.projection == "public"
    private = next(e for e in owner.embers if e.value_kind == "private_handle")
    assert private.ref not in public.to_json()
    for ref in ("syn://type/wallet-agent", "synny://type/wallet-agent"):
        result = service.list_objects(
            app_env.context, snapshot_id, type_ref=ref, include_subtypes=True
        )
        assert [row["ref"] for row in result["items"]] == [view.agent_ref]
    cogs = service.list_objects(
        app_env.context,
        snapshot_id,
        type_ref="synny://type/locking-cog",
        include_subtypes=True,
        primitive_kind="cog",
    )
    assert [row["ref"] for row in cogs["items"]] == [view.locking_cog_ref]
    with app_env.db.transaction(app_env.context) as connection:
        artifacts_rows = connection.execute("SELECT id FROM artifacts").fetchall()
        source_rows = connection.execute("SELECT metadata FROM embedding_sources").fetchall()
    for row in artifacts_rows:
        data = artifacts.read(app_env.context, row["id"])
        assert private.ref.encode() not in data
        assert private.value.encode() not in data
        assert private.revision.encode() not in data
    assert private.ref not in str(source_rows)
