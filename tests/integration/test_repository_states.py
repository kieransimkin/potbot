"""P022–P025 behavior using real Git, filesystem snapshots and PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from potbot.domain.configuration import resolve_configuration
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.evaluation.repository_fixture import FixtureVariant, create_repository_fixture
from potbot.models.configuration import EnvironmentManifest, store_configuration, store_environment
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.persistence.database import Database, RepositoryStore
from potbot.tools.repository import (
    CandidateWorkspaceManager,
    RepositoryStateStore,
    SnapshotProfile,
    assert_lease,
    workspace_fingerprint,
)

pytestmark = pytest.mark.postgres


def environment(app_env) -> UUID:
    configuration = resolve_configuration({})
    with app_env.db.transaction(app_env.context) as connection:
        saved = store_configuration(connection, app_env.context, configuration)
        return store_environment(
            connection,
            app_env.context,
            EnvironmentManifest(
                saved["id"],
                configuration.fingerprint,
                "linux",
                "Python-3.12",
                (("git", "fixture"),),
            ),
        )["id"]


def setup(app_env, tmp_path: Path):
    fixture = create_repository_fixture(tmp_path / "source", FixtureVariant.DIRTY_TRACKED)
    artifacts = ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root))
    states = RepositoryStateStore(app_env.db, artifacts)
    env = environment(app_env)
    state = states.capture(app_env.context, app_env.repository_id, fixture.path, env)
    manager = CandidateWorkspaceManager(app_env.db, artifacts, tmp_path / "candidates")
    return fixture, artifacts, states, env, state, manager


def test_states_keep_distinct_commits_parents_environment_and_artifacts_after_restart(
    app_env, tmp_path: Path
) -> None:
    fixture, artifacts, states, env, first, _ = setup(app_env, tmp_path)
    left = states.capture(
        app_env.context,
        app_env.repository_id,
        fixture.path,
        env,
        committed_ref="candidate-left",
        parent_ids=(first["id"],),
    )
    right = states.capture(
        app_env.context,
        app_env.repository_id,
        fixture.path,
        env,
        committed_ref="candidate-right",
        parent_ids=(first["id"],),
    )
    assert left["commit_id"] == fixture.left_commit and right["commit_id"] == fixture.right_commit
    assert left["content_digest"] != right["content_digest"]
    assert left["environment_id"] == env
    assert left["parent_ids"] == [first["id"]]
    restarted = RepositoryStateStore(Database(app_env.db.dsn), artifacts)
    assert restarted.get(app_env.context, left["id"])["identity_hash"] == left["identity_hash"]
    assert (
        restarted.snapshot(app_env.context, first["id"]).content_digest == first["content_digest"]
    )
    assert (
        states.capture(app_env.context, app_env.repository_id, fixture.path, env)["id"]
        == first["id"]
    )
    with app_env.db.transaction(app_env.context) as connection:
        parents = connection.execute(
            "SELECT parent_id FROM repository_state_parents WHERE state_id=%s", (left["id"],)
        ).fetchall()
        assert parents == [{"parent_id": first["id"]}]


def test_lineage_scope_cycle_and_published_history_cannot_be_changed(
    app_env, tmp_path: Path
) -> None:
    fixture, _, states, env, first, _ = setup(app_env, tmp_path)
    second = states.capture(
        app_env.context,
        app_env.repository_id,
        fixture.path,
        env,
        committed_ref="candidate-left",
        parent_ids=(first["id"],),
    )
    another = RepositoryStore(app_env.db).create(
        app_env.context, name="same-workspace-other-repo", source_uri="fixture://other"
    )
    with pytest.raises(ScopeDeniedError):
        states.capture(app_env.context, another["id"], fixture.path, env, parent_ids=(first["id"],))
    with pytest.raises(InvalidInputError):
        with app_env.db.transaction(app_env.context) as connection:
            connection.execute(
                "INSERT INTO repository_state_parents(workspace_id,repository_id,state_id,parent_id) VALUES(%s,%s,%s,%s)",
                (app_env.context.workspace_id, app_env.repository_id, first["id"], second["id"]),
            )
    with pytest.raises(ScopeDeniedError):
        with app_env.db.transaction(app_env.context) as connection:
            connection.execute(
                "UPDATE repository_states SET parent_ids=%s WHERE id=%s",
                ([second["id"]], first["id"]),
            )
    with pytest.raises(ScopeDeniedError):
        RepositoryStateStore(app_env.other_db, states.artifacts).get(
            app_env.other_context, first["id"]
        )
    with pytest.raises(ScopeDeniedError):
        states.capture(app_env.context, app_env.other_repository_id, fixture.path, env)
    with pytest.raises(ScopeDeniedError):
        states.capture(app_env.context, app_env.repository_id, fixture.path, uuid4())
    assert states.get(app_env.context, first["id"])["parent_ids"] == []


def test_candidates_are_independent_and_cleanup_proves_ownership(app_env, tmp_path: Path) -> None:
    fixture, artifacts, _, _, first, manager = setup(app_env, tmp_path)
    left = manager.allocate(app_env.context, first["id"])
    right = manager.allocate(app_env.context, first["id"])
    source = (fixture.path / "calculator.py").read_text()
    left_path, right_path = Path(left["path"]), Path(right["path"])
    (left_path / "calculator.py").write_text("# conflicting left edit\n")
    (right_path / "calculator.py").write_text("# conflicting right edit\n")
    assert (fixture.path / "calculator.py").read_text() == source
    marker = left_path.parent / "owner.json"
    original = marker.read_bytes()
    marker.write_text("{}")
    with pytest.raises(StateMismatchError):
        manager.cleanup(app_env.context, left["id"])
    marker.write_bytes(original)
    outside = tmp_path / "caller-owned.txt"
    outside.write_text("retained")
    (left_path / "external-link").symlink_to(outside)
    manager.cleanup(app_env.context, left["id"])
    assert not left_path.exists() and right_path.exists()
    assert outside.read_text() == "retained" and fixture.sentinel_path.exists()
    assert artifacts.read(app_env.context, first["snapshot_artifact_id"])
    with pytest.raises(StateMismatchError):
        manager.get(app_env.context, left["id"])


def test_one_concurrent_mutation_owner_and_fenced_publication(app_env, tmp_path: Path) -> None:
    _, _, states, _, first, manager = setup(app_env, tmp_path)
    candidate = manager.allocate(app_env.context, first["id"])

    def claim(owner: str):
        try:
            return manager.claim(app_env.context, candidate["id"], owner)
        except StateMismatchError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("worker-a", "worker-b")))
    winners = [value for value in claims if value is not None]
    assert len(winners) == 1
    lease = winners[0]
    with pytest.raises(StateMismatchError):
        manager.cleanup(app_env.context, candidate["id"])
    with app_env.db.transaction(app_env.context) as connection:
        assert assert_lease(connection, app_env.context, lease)["state_id"] == first["id"]
    (Path(candidate["path"]) / "calculator.py").write_text("# completed mutation\n")
    result = manager.publish_state(app_env.context, lease)
    assert result["id"] != first["id"] and result["parent_ids"] == [first["id"]]
    assert result["manifest"]["source_kind"] == "candidate"
    assert states.snapshot(app_env.context, result["id"]).content_digest == workspace_fingerprint(
        Path(candidate["path"])
    )
    manager.release(app_env.context, lease)
    new_lease = manager.claim(app_env.context, candidate["id"], "new-worker")
    assert new_lease.generation == lease.generation + 1
    with pytest.raises(StateMismatchError):
        manager.publish_state(app_env.context, lease)
    assert manager.check_lease(app_env.context, new_lease)["state_id"] == result["id"]


def test_expiration_quarantines_until_stopped_process_and_content_reconciliation(
    app_env, tmp_path: Path
) -> None:
    _, _, _, _, first, manager = setup(app_env, tmp_path)
    candidate = manager.allocate(app_env.context, first["id"])
    lease = manager.claim(app_env.context, candidate["id"], "old-process")
    with app_env.db.transaction(app_env.context) as connection:
        connection.execute(
            "UPDATE workspace_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE candidate_id=%s",
            (candidate["id"],),
        )
    with pytest.raises(StateMismatchError):
        manager.publish_state(app_env.context, lease)
    with pytest.raises(StateMismatchError):
        manager.claim(app_env.context, candidate["id"], "unsafe-replacement")
    assert manager.get(app_env.context, candidate["id"])["status"] == "quarantined"
    with pytest.raises(InvalidInputError):
        manager.reconcile(
            app_env.context,
            candidate["id"],
            expected_generation=lease.generation,
            observed_digest=first["content_digest"],
            reason="unknown process",
            processes_stopped=False,
        )
    with pytest.raises(StateMismatchError):
        manager.reconcile(
            app_env.context,
            candidate["id"],
            expected_generation=lease.generation,
            observed_digest="0" * 64,
            reason="wrong content",
            processes_stopped=True,
        )
    decision = manager.reconcile(
        app_env.context,
        candidate["id"],
        expected_generation=lease.generation,
        observed_digest=first["content_digest"],
        reason="Supervisor confirmed exit; exact bytes inspected",
        processes_stopped=True,
    )
    assert decision["generation"] == lease.generation
    replacement = manager.claim(app_env.context, candidate["id"], "reconciled-owner")
    assert replacement.generation > lease.generation
    with pytest.raises(StateMismatchError):
        manager.quarantine(app_env.context, lease)
    manager.release(app_env.context, replacement)


def test_renewal_reload_and_ownership_root_mismatch_fail_closed(app_env, tmp_path: Path) -> None:
    _, artifacts, _, _, first, manager = setup(app_env, tmp_path)
    candidate = manager.allocate(app_env.context, first["id"])
    lease = manager.claim(app_env.context, candidate["id"], "owner", ttl_seconds=1)
    renewed = manager.renew(app_env.context, lease, ttl_seconds=60)
    assert renewed.generation == lease.generation and renewed.expires_at > lease.expires_at
    restarted = CandidateWorkspaceManager(Database(app_env.db.dsn), artifacts, manager.root)
    assert restarted.check_lease(app_env.context, renewed)["id"] == candidate["id"]
    other_root = CandidateWorkspaceManager(app_env.db, artifacts, tmp_path / "other-root")
    with pytest.raises(StateMismatchError):
        other_root.get(app_env.context, candidate["id"])
    with pytest.raises(ScopeDeniedError):
        manager.get(app_env.other_context, candidate["id"])
    restarted.release(app_env.context, renewed)


def test_generated_and_excluded_inputs_keep_durable_snapshot_identities(
    app_env, tmp_path: Path
) -> None:
    fixture, _, states, env, _, manager = setup(app_env, tmp_path)
    (fixture.path / "runtime_offset.txt").write_text("0\n")
    (fixture.path / "private.txt").write_text("DO_NOT_COPY_THIS")
    profile = SnapshotProfile(
        untracked_paths=("runtime_offset.txt", "private.txt"),
        excluded_paths=("private.txt",),
        secret_refs=("secret://fixture/key",),
    )
    first = states.capture(
        app_env.context, app_env.repository_id, fixture.path, env, profile=profile
    )
    (fixture.path / "runtime_offset.txt").write_text("8\n")
    second = states.capture(
        app_env.context,
        app_env.repository_id,
        fixture.path,
        env,
        profile=profile,
        parent_ids=(first["id"],),
    )
    assert first["id"] != second["id"] and first["content_digest"] != second["content_digest"]
    candidate = manager.allocate(app_env.context, first["id"])
    assert (Path(candidate["path"]) / "runtime_offset.txt").read_text() == "0\n"
    assert not (Path(candidate["path"]) / "private.txt").exists()
    assert "DO_NOT_COPY_THIS" not in states.snapshot(app_env.context, first["id"]).payload.decode()
