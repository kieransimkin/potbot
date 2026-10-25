"""Deterministic concurrency footprint and logical ownership tests (not OS security proof)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools.agent_homes import AgentHomeRegistry, default_agent_directory
from potbot.tools.agent_permissions import (
    COLLABORATIVE_FILE_MODE,
    DEFAULT_DIRECTORY_MODE,
    DEFAULT_FILE_MODE,
    FIRST_AGENT_UID,
    SHARED_HOME_STICKY_MODE,
    WORKSPACE_GID,
    SimulatedAgentCredentials,
    posix_mode_allows,
    safe_agent_rename_allowed,
)
from potbot.tools.concurrency_profiles import (
    classify_home_path,
    directory_footprint,
    file_footprint,
    model_artifact_footprint,
    package_footprint,
    sqlite_footprint,
    unknown_shell_footprint,
)
from potbot.tools.github_concurrency import GitHubChangeIntent, GitHubFileChangePlan, GitHubFileVersion
from potbot.tools.workspace_concurrency import (
    AccessRequest,
    AccessResource,
    WorkspaceLeaseLedger,
    conflict,
)

DIGEST = "e" * 64
QUAL = "a" * 64


@pytest.fixture
def host(tmp_path: Path):
    home = tmp_path / "workspaces" / "home"
    home.mkdir(parents=True)
    private = tmp_path / "private"
    private.mkdir()
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ledger = WorkspaceLeaseLedger(private / "controller.sqlite", workspace_home=home)
    workspace_id = uuid4()
    ledger.pin_image(
        workspace_id, digest=DIGEST, image_ref="ghcr.io/owner/image@sha256:" + DIGEST,
        build_commit="f" * 40, platform="linux/amd64", rootfs_path=rootfs,
        qualification_sha256=QUAL,
    )
    return ledger, workspace_id


def request(ws, actor, *, idempotency, resources, effect="write"):
    return AccessRequest(ws, actor, "test.exact_access", idempotency, effect, resources, DIGEST)


def test_disjoint_file_edits_and_same_file_conflicts(host):
    ledger, ws = host
    files_a = file_footprint("file_edit", ("/home/project/a.py",))
    files_b = file_footprint("file_edit", ("/home/project/b.py",))
    first = ledger.claim(request(ws, uuid4(), idempotency="a", resources=files_a.resources))
    second = ledger.claim(request(ws, uuid4(), idempotency="b", resources=files_b.resources))
    with pytest.raises(StateMismatchError):
        ledger.claim(request(ws, uuid4(), idempotency="c", resources=files_a.resources))
    ledger.finish(first, result_sha256=QUAL)
    ledger.finish(second, result_sha256=QUAL)


def test_package_installs_same_env_serialize_even_when_lockfile_reads(host):
    ledger, ws = host
    first = package_footprint("/home/stemlab", manager="npm")
    second = package_footprint("/home/stemlab", manager="npm")
    parallel = package_footprint("/home/framemoves", manager="npm")
    a = ledger.claim(request(ws, uuid4(), idempotency="npm-a", resources=first.resources))
    with pytest.raises(StateMismatchError):
        ledger.claim(request(ws, uuid4(), idempotency="npm-b", resources=second.resources))
    b = ledger.claim(request(ws, uuid4(), idempotency="other", resources=parallel.resources))
    ledger.finish(a, result_sha256=QUAL)
    ledger.finish(b, result_sha256=QUAL)


def test_package_lock_regeneration_conflicts_with_install(host):
    ledger, ws = host
    regular = package_footprint("/home/potbot", manager="uv")
    refresh = package_footprint("/home/potbot", manager="uv", change_lock=True)
    lease = ledger.claim(request(ws, uuid4(), idempotency="install", resources=regular.resources))
    with pytest.raises(StateMismatchError):
        ledger.claim(request(ws, uuid4(), idempotency="refresh", resources=refresh.resources))
    ledger.finish(lease, result_sha256=QUAL)


def test_model_downloads_by_digest_and_readers(host):
    ledger, ws = host
    different = model_artifact_footprint("/home/PixelCue/models", digest="b" * 64, writer=True)
    same = model_artifact_footprint("/home/PixelCue/models", digest="c" * 64, writer=True)
    a = ledger.claim(request(ws, uuid4(), idempotency="model-a", resources=different.resources))
    b = ledger.claim(request(ws, uuid4(), idempotency="model-b", resources=same.resources))
    with pytest.raises(StateMismatchError):
        ledger.claim(request(ws, uuid4(), idempotency="model-c", resources=different.resources))
    ledger.finish(a, result_sha256=QUAL)
    ledger.finish(b, result_sha256=QUAL)
    readers = model_artifact_footprint("/home/PixelCue/models", digest="c" * 64, writer=False)
    one = ledger.claim(request(ws, uuid4(), idempotency="read-1", resources=readers.resources,
                                     effect="read"))
    two = ledger.claim(request(ws, uuid4(), idempotency="read-2", resources=readers.resources,
                                     effect="read"))
    ledger.finish(one, result_sha256=QUAL)
    ledger.finish(two, result_sha256=QUAL)


def test_sqlite_wal_shm_family_serializes(host):
    ledger, ws = host
    a = sqlite_footprint("/home/keywordmoves/watch.sqlite", write=True)
    b = sqlite_footprint("/home/keywordmoves/watch.sqlite-wal", write=False)
    lease = ledger.claim(request(ws, uuid4(), idempotency="sql", resources=a.resources))
    with pytest.raises(StateMismatchError):
        ledger.claim(request(ws, uuid4(), idempotency="wal", resources=b.resources,
                             effect="read"))
    ledger.finish(lease, result_sha256=QUAL)


def test_directories_and_unknown_shell_conflicts(host):
    ledger, ws = host
    a = directory_footprint("/home/workspace-a/outputs", kind="media_render")
    b = directory_footprint("/home/workspace-b/outputs", kind="media_render")
    lease_a = ledger.claim(request(ws, uuid4(), idempotency="render-a", resources=a.resources))
    lease_b = ledger.claim(request(ws, uuid4(), idempotency="render-b", resources=b.resources))
    with pytest.raises(StateMismatchError):
        ledger.claim(request(ws, uuid4(), idempotency="shell", effect="unknown",
                             resources=unknown_shell_footprint().resources))
    ledger.finish(lease_a, result_sha256=QUAL)
    ledger.finish(lease_b, result_sha256=QUAL)


def test_github_file_edits_disjoint_but_branch_cas_exclusive():
    base = "owner/repo#refs/heads/potbot/actor"
    first = AccessResource("github_file", base + ":src/a.py", "write")
    second = AccessResource("github_file", base + ":src/b.py", "write")
    branch = AccessResource("github", base, "write")
    assert not conflict(first, second, same_workspace=False)
    assert conflict(first, branch, same_workspace=False)
    assert conflict(first, AccessResource("github_file", base + ":src/a.py", "read"),
                    same_workspace=False)
    with pytest.raises(InvalidInputError):
        AccessResource("github_file", base + ":../secrets", "write")


def test_github_file_plan_requires_base_blob_and_agent_branch():
    ws, agent = uuid4(), uuid4()
    branch = f"potbot/{ws.hex[:12]}/{agent.hex[:12]}"
    intent = GitHubChangeIntent(ws, agent, "owner/repo", branch, "a" * 40, "d" * 64)
    plan = GitHubFileChangePlan(intent, (
        GitHubFileVersion("src/a.py", "e" * 40),
        GitHubFileVersion("src/b.py", None),
    ))
    assert len(plan.access_request(image_digest=DIGEST, idempotency_key="gh-files").resources) == 2
    with pytest.raises(InvalidInputError):
        GitHubFileChangePlan(intent, (GitHubFileVersion("src/a.py", None),) * 2)


def test_source_derived_resource_categories():
    table = {
        "/home/stemlab/package-lock.json": "package-lock",
        "/home/likenessforge/uv.lock": "package-lock",
        "/home/pixelcue/models/checkpoint.bin": "shared-cache-or-model",
        "/home/frame/outputs/movie.mp4": "build-output",
        "/home/keywordmoves/watch.sqlite-wal": "sqlite-store",
        "/home/potbot/memory/graph.db": "sqlite-store",
        "/home/demo/.git/index.lock": "git-metadata",
        "/home/unity/Library/ArtifactDB": "build-output",
    }
    for path, expected in table.items():
        assert classify_home_path(path) == expected
    with pytest.raises(ScopeDeniedError):
        file_footprint("file_edit", ("/etc/passwd",))


def test_allocated_agent_uids_and_rename(host):
    ledger, ws = host
    registry = AgentHomeRegistry(ledger, workspace_name="DanceFlow Studio")
    alice, bob = uuid4(), uuid4()
    one = registry.provision(ws, alice)
    two = registry.provision(ws, bob)
    assert (one.uid, two.uid) == (420, 421)
    assert (one.gid, two.gid) == (69, 69)
    assert one.group_name == "danceflow-studio"
    assert one.directory == default_agent_directory(alice)
    assert (ledger.home / one.directory).stat().st_mode & 0o777 == 0o775
    renamed = registry.rename(ws, alice, "coder-agent", expected_revision=1)
    assert renamed.uid == one.uid and renamed.directory == "coder-agent"
    assert registry.lookup(ws, alice) == renamed
    with pytest.raises(StateMismatchError):
        registry.rename(ws, alice, "another", expected_revision=1)
    with pytest.raises(StateMismatchError):
        registry.rename(ws, bob, "coder-agent", expected_revision=1)


def test_simulated_modes_do_not_claim_os_isolation():
    owner = SimulatedAgentCredentials(FIRST_AGENT_UID, WORKSPACE_GID, "danceflow")
    peer = SimulatedAgentCredentials(FIRST_AGENT_UID + 1, WORKSPACE_GID, "danceflow")
    assert DEFAULT_DIRECTORY_MODE == 0o775
    assert DEFAULT_FILE_MODE == 0o644
    assert posix_mode_allows(mode=0o775, owner_uid=owner.uid, group_gid=69,
                             actor=peer, permission="write")
    assert not posix_mode_allows(mode=0o755, owner_uid=owner.uid, group_gid=69,
                                 actor=peer, permission="write")
    assert not posix_mode_allows(mode=DEFAULT_FILE_MODE, owner_uid=owner.uid,
                                 group_gid=69, actor=peer, permission="write")
    assert posix_mode_allows(mode=COLLABORATIVE_FILE_MODE, owner_uid=owner.uid,
                             group_gid=69, actor=peer, permission="write")
    assert not safe_agent_rename_allowed(parent_mode=SHARED_HOME_STICKY_MODE,
                                        owner_uid=1, actor_uid=peer.uid,
                                        entry_owner_uid=owner.uid)


def test_shared_file_broker_cas_and_644_default(host):
    from potbot.tools.shared_file_broker import SharedFileBroker

    ledger, ws = host
    agent = uuid4()
    group = AgentHomeRegistry(ledger, workspace_name="danceflow")
    own = group.provision(ws, agent)
    project = ledger.home / own.directory / "src"
    project.mkdir(mode=0o775)
    project.chmod(0o775)
    broker = SharedFileBroker(
        ledger=ledger, agent_homes=group,
        authorize=lambda workspace, actor: workspace == ws and actor == agent,
    )
    path = own.guest_path + "/src/file.py"
    result = broker.replace(
        workspace_id=ws, actor_id=agent, image_digest=DIGEST, guest_path=path,
        expected_sha256=None, data=b"print(42)\n", idempotency_key="create-1",
    )
    assert result.before_sha256 is None
    assert (project / "file.py").read_bytes() == b"print(42)\n"
    assert (project / "file.py").stat().st_mode & 0o777 == 0o644
    with pytest.raises(StateMismatchError):
        broker.replace(
            workspace_id=ws, actor_id=agent, image_digest=DIGEST, guest_path=path,
            expected_sha256=None, data=b"print(99)\n", idempotency_key="create-2",
        )
    assert (project / "file.py").read_bytes() == b"print(42)\n"
    updated = broker.replace(
        workspace_id=ws, actor_id=agent, image_digest=DIGEST, guest_path=path,
        expected_sha256=result.after_sha256, data=b"print(99)\n", idempotency_key="update-1",
    )
    assert updated.before_sha256 == result.after_sha256
    assert (project / "file.py").read_bytes() == b"print(99)\n"


def test_shared_file_broker_respects_peer_755_directory(host):
    from potbot.tools.shared_file_broker import SharedFileBroker

    ledger, ws = host
    alice, bob = uuid4(), uuid4()
    identities = AgentHomeRegistry(ledger, workspace_name="danceflow")
    alice_home = identities.provision(ws, alice)
    identities.provision(ws, bob)
    broker = SharedFileBroker(
        ledger=ledger, agent_homes=identities,
        authorize=lambda workspace, actor: workspace == ws and actor in {alice, bob},
    )
    path = alice_home.guest_path + "/note.txt"
    # Shared 775 directory means peers can replace a 644 file atomically.
    first = broker.replace(
        workspace_id=ws, actor_id=alice, image_digest=DIGEST, guest_path=path,
        expected_sha256=None, data=b"hi", idempotency_key="own-note",
    )
    peer = broker.replace(
        workspace_id=ws, actor_id=bob, image_digest=DIGEST, guest_path=path,
        expected_sha256=first.after_sha256, data=b"hello", idempotency_key="peer-note",
    )
    assert peer.before_sha256 == first.after_sha256
    # Removing group-write permission from a directory blocks new peer edits.
    (ledger.home / alice_home.directory).chmod(0o755)
    with pytest.raises(ScopeDeniedError):
        broker.replace(
            workspace_id=ws, actor_id=bob, image_digest=DIGEST, guest_path=path,
            expected_sha256=peer.after_sha256, data=b"not permitted", idempotency_key="deny-note",
        )
    # The owning agent still has permission to update the file.
    broker.replace(
        workspace_id=ws, actor_id=alice, image_digest=DIGEST, guest_path=path,
        expected_sha256=peer.after_sha256, data=b"owner ok", idempotency_key="owner-note",
    )


def test_shared_file_broker_does_not_follow_symlinks(host):
    from potbot.tools.shared_file_broker import SharedFileBroker

    ledger, ws = host
    actor = uuid4()
    identities = AgentHomeRegistry(ledger, workspace_name="danceflow")
    owner = identities.provision(ws, actor)
    own = ledger.home / owner.directory
    (own / "shortcut").symlink_to(ledger.home.parent)
    broker = SharedFileBroker(
        ledger=ledger, agent_homes=identities,
        authorize=lambda workspace, agent: workspace == ws and agent == actor,
    )
    with pytest.raises((OSError, ScopeDeniedError)):
        broker.replace(
            workspace_id=ws, actor_id=actor, image_digest=DIGEST,
            guest_path=owner.guest_path + "/shortcut/outside.txt",
            expected_sha256=None, data=b"unexpected", idempotency_key="symlink-parent",
        )
    (own / "target.txt").symlink_to(ledger.home.parent / "private")
    with pytest.raises((OSError, ScopeDeniedError)):
        broker.replace(
            workspace_id=ws, actor_id=actor, image_digest=DIGEST,
            guest_path=owner.guest_path + "/target.txt",
            expected_sha256=None, data=b"unexpected", idempotency_key="symlink-target",
        )


def test_danceflow_hazard_catalogue_covers_every_component_and_hub():
    from potbot.tools.danceflow_concurrency_catalog import DANCEFLOW_RISKS

    names = [entry.name for entry in DANCEFLOW_RISKS]
    assert len(names) == 18
    assert len(set(names)) == 18
    assert names.count("ThumbMoves") == 1
    assert any("uv.lock" in r.package_locks for r in DANCEFLOW_RISKS if r.name == "Potbot")
    assert any("Library/" in r.mutable_areas for r in DANCEFLOW_RISKS if r.name == "DanceFlow for Unity")
    assert any("SQLite" in e for r in DANCEFLOW_RISKS if r.name == "KeywordMoves" for e in r.global_effects)
    assert all(len(r.source_commit) == 40 and len(r.repository.split("/")) == 2 for r in DANCEFLOW_RISKS)


def test_global_package_cache_conflicts_across_two_workspaces(host, tmp_path: Path):
    ledger, first_workspace = host
    second_workspace = uuid4()
    ledger.pin_image(
        second_workspace, digest=DIGEST,
        image_ref="ghcr.io/owner/image@sha256:" + DIGEST,
        build_commit="f" * 40, platform="linux/amd64",
        rootfs_path=tmp_path / "rootfs", qualification_sha256=QUAL,
    )
    first = package_footprint("/home/stemlab", manager="uv", shared_cache="cache:uv/shared")
    second = package_footprint("/home/rastermoves", manager="uv", shared_cache="cache:uv/shared")
    a = ledger.claim(request(first_workspace, uuid4(), idempotency="shared-cache-a",
                             resources=first.resources))
    with pytest.raises(StateMismatchError):
        ledger.claim(request(second_workspace, uuid4(), idempotency="shared-cache-b",
                             resources=second.resources))
    ledger.finish(a, result_sha256=QUAL)
    b = ledger.claim(request(second_workspace, uuid4(), idempotency="shared-cache-c",
                             resources=second.resources))
    ledger.finish(b, result_sha256=QUAL)


def test_memory_promotion_conflicts_only_for_same_index(host):
    ledger, ws = host
    first = directory_footprint("/home/shared/memories/index-v7", kind="memory_update")
    other = directory_footprint("/home/shared/memories/index-v8", kind="memory_update")
    a = ledger.claim(request(ws, uuid4(), idempotency="memory-a", resources=first.resources))
    b = ledger.claim(request(ws, uuid4(), idempotency="memory-b", resources=other.resources))
    with pytest.raises(StateMismatchError):
        ledger.claim(request(ws, uuid4(), idempotency="memory-c", resources=first.resources))
    ledger.finish(a, result_sha256=QUAL)
    ledger.finish(b, result_sha256=QUAL)
