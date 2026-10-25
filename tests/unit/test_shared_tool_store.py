"""S4 shared tool source, maintenance, reuse and agent edit-plan contracts."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools.shared_tool_store import (
    SHARED_ARTIFACT_POLICIES,
    SharedToolDefinition,
    SharedToolStore,
)
from potbot.tools.workspace_concurrency import AccessRequest, AccessResource, WorkspaceLeaseLedger

IMAGE = "a" * 64


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)
    return result.stdout.decode().strip()


@pytest.fixture
def store(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    image = tmp_path / "image"
    image.mkdir()
    workspace, alice, bob = uuid4(), uuid4(), uuid4()
    ledger = WorkspaceLeaseLedger(private / "leases.sqlite", workspace_home=home)
    ledger.pin_image(workspace, digest=IMAGE, image_ref="ghcr.io/test/root@sha256:" + IMAGE,
                     build_commit="c" * 40, platform="linux/amd64",
                     rootfs_path=image, qualification_sha256="f" * 64)
    manager = SharedToolStore(ledger=ledger, shared_root=tools,
                              authorize=lambda ws, actor: ws == workspace and actor in {alice, bob})
    source = tools / "stemlab"
    checkout = source / "checkout"
    checkout.mkdir(parents=True)
    (source / "environment").mkdir()
    (source / "static").mkdir()
    (source / "downloads").mkdir()
    git(checkout, "init", "-q")
    git(checkout, "config", "user.email", "example@example.org")
    git(checkout, "config", "user.name", "Test")
    (checkout / "source.py").write_text("print('hello')\n")
    (checkout / "package-lock.json").write_text('{"lockfileVersion":3}\n')
    (checkout / ".gitignore").write_text("build/\n.pytest_cache/\n")
    git(checkout, "add", ".")
    git(checkout, "commit", "-qm", "init")
    git(checkout, "remote", "add", "origin", "https://github.com/kieransimkin/stemlab")
    commit = git(checkout, "rev-parse", "HEAD")
    lock_sha = hashlib.sha256((checkout / "package-lock.json").read_bytes()).hexdigest()
    definition = SharedToolDefinition(
        "stemlab", "https://github.com/kieransimkin/stemlab", commit,
        ("build", ".pytest_cache"), (("package-lock.json", lock_sha),), True,
    )
    from potbot.tools.model_cache_routes import MODEL_ENV

    manager.register(workspace_id=workspace, agent_id=alice, definition=definition,
                     idempotency_key="register-one", install_probe={
        "tool": "stemlab", "network_used": False, "observed": True,
        "environment": dict(MODEL_ENV),
        "model_destinations": ["/var/ai/cache/huggingface/hub/model.safetensors"],
        "writes": ["/usr/local/tools/stemlab/environment/bin/entry",
                   "/var/ai/cache/huggingface/hub/model.safetensors"],
        "cleanup_test_passed": True, "normal_use_test_passed": True,
    })
    return manager, ledger, workspace, alice, bob, home, checkout, source


def test_shared_tool_reads_overlap_but_clean_requires_exclusive(store) -> None:
    manager, ledger, ws, alice, bob, home, checkout, source = store
    with manager.use(workspace_id=ws, agent_id=alice, slug="stemlab", idempotency_key="use-1") as path:
        assert path == checkout
        with manager.use(workspace_id=ws, agent_id=bob, slug="stemlab", idempotency_key="use-2"):
            with pytest.raises(StateMismatchError):
                manager.clean_for_edit(workspace_id=ws, agent_id=alice,
                                       slug="stemlab", idempotency_key="clean-blocked")
        assert ledger.active(ws)[0]["operation"] == "tool.use"
    assert not ledger.active(ws)
    with manager.use(workspace_id=ws, agent_id=alice, slug="stemlab", idempotency_key="use-3"):
        # Unknown shell effects still take a workspace-wide lock; no bypass.
        with pytest.raises(StateMismatchError):
            ledger.claim(AccessRequest(ws, bob, "shell.execute", "console-1", "unknown",
                                       (AccessResource("workspace", "*", "write"),), IMAGE))


def test_shared_tool_clean_preserves_reusable_assets(store) -> None:
    manager, _, ws, alice, _, _, checkout, source = store
    (source / "environment" / "bin").mkdir()
    (source / "environment" / "bin" / "python").write_bytes(b"pinned environment")
    (source / "static" / "render.so").write_bytes(b"verified build output")
    (source / "downloads" / "numpy.whl").write_bytes(b"download archive")
    (checkout / "build").mkdir()
    (checkout / "build" / "junk.o").write_bytes(b"object")
    (checkout / ".pytest_cache").mkdir()
    (checkout / ".pytest_cache" / "lastfailed").write_text("failed")
    result = manager.clean_for_edit(workspace_id=ws, agent_id=alice,
                                    slug="stemlab", idempotency_key="clean-first")
    assert result.removed == ("build", ".pytest_cache")
    assert result.source_clean and result.static_artifacts_preserved
    assert git(checkout, "status", "--porcelain") == ""
    for relative in ("environment/bin/python", "static/render.so", "downloads/numpy.whl"):
        assert (source / relative).is_file()
    assert manager.clean_for_edit(workspace_id=ws, agent_id=alice,
                                  slug="stemlab", idempotency_key="clean-again").removed == ()


def test_unknown_untracked_and_modified_source_never_deleted(store) -> None:
    manager, _, ws, alice, _, _, checkout, _ = store
    (checkout / "not-in-manifest.txt").write_text("user work")
    with pytest.raises(StateMismatchError):
        manager.clean_for_edit(workspace_id=ws, agent_id=alice,
                               slug="stemlab", idempotency_key="unknown-first")
    assert (checkout / "not-in-manifest.txt").read_text() == "user work"
    (checkout / "not-in-manifest.txt").unlink()
    (checkout / "source.py").write_text("modified\n")
    with pytest.raises(StateMismatchError):
        manager.clean_for_edit(workspace_id=ws, agent_id=alice,
                               slug="stemlab", idempotency_key="tracked-changed")
    assert (checkout / "source.py").read_text() == "modified\n"


def test_unknown_ignored_and_symlinked_output_refused(store, tmp_path: Path) -> None:
    manager, _, ws, alice, _, _, checkout, _ = store
    (checkout / ".pytest_cache").mkdir()
    (checkout / ".pytest_cache" / "x").write_text("yes")
    (checkout / "build").symlink_to(tmp_path)
    with pytest.raises(ScopeDeniedError):
        manager.clean_for_edit(workspace_id=ws, agent_id=alice,
                               slug="stemlab", idempotency_key="bad-link")
    assert tmp_path.is_dir()
    assert (checkout / "build").is_symlink()


def test_clone_plan_after_cleanup_is_agent_owned(store) -> None:
    manager, _, ws, alice, _, home, checkout, _ = store
    agent = home / "alice"
    (agent / "projects").mkdir(parents=True)
    plan = manager.edit_checkout_plan(workspace_id=ws, agent_id=alice,
                                      slug="stemlab", agent_home=agent,
                                      idempotency_key="clone-1")
    assert plan["source"] == "/usr/local/tools/stemlab/checkout"
    assert plan["destination"] == "/home/alice/projects/stemlab"
    assert "--no-local" in plan["git_command"]
    (agent / "projects/stemlab").mkdir()
    with pytest.raises(StateMismatchError):
        manager.edit_checkout_plan(workspace_id=ws, agent_id=alice,
                                    slug="stemlab", agent_home=agent,
                                    idempotency_key="clone-2")
    assert git(checkout, "status", "--porcelain") == ""


def test_operator_approval_and_manifest_input_required(store) -> None:
    manager, _, ws, alice, bob, home, _, _ = store
    with pytest.raises(ScopeDeniedError):
        with manager.use(workspace_id=uuid4(), agent_id=alice, slug="stemlab",
                         idempotency_key="foreign"):
            pass
    with pytest.raises(StateMismatchError):
        with manager.use(workspace_id=ws, agent_id=bob, slug="not-installed",
                         idempotency_key="unknown"):
            pass
    for path in ("../bad", "/etc", ".git", "node_modules", "models", ".venv"):
        with pytest.raises((ScopeDeniedError, InvalidInputError)):
            SharedToolDefinition("stemlab", "https://github.com/kieransimkin/stemlab", "a" * 40,
                                 (path,))
    assert len(SHARED_ARTIFACT_POLICIES) >= 8


def test_write_lock_on_tool_does_not_block_other_tool(store) -> None:
    manager, ledger, ws, alice, bob, _, _, _ = store
    ticket = ledger.claim(AccessRequest(ws, alice, "tool.upgrade", "upgrade-future", "write",
                                        (AccessResource("shared_tool", "stemlab", "write"),), IMAGE))
    try:
        other = ledger.claim(AccessRequest(ws, bob, "tool.use", "danceflows", "read",
                                           (AccessResource("shared_tool", "dancemoves", "read"),), IMAGE))
        ledger.finish(other, result_sha256="0" * 64)
        with pytest.raises(StateMismatchError):
            with manager.use(workspace_id=ws, agent_id=bob, slug="stemlab",
                             idempotency_key="use-locked"):
                pass
    finally:
        ledger.finish(ticket, result_sha256="1" * 64)


def test_clone_for_edit_executes_under_one_maintenance_lease(store) -> None:
    manager, ledger, ws, alice, bob, home, checkout, _ = store
    agent = home / "alice"
    (agent / "projects").mkdir(parents=True)
    (checkout / "build").mkdir()
    (checkout / "build" / "temp.bin").write_bytes(b"ephemeral")
    calls = []

    def admitted_clone(plan: dict[str, str]) -> None:
        calls.append(plan)
        with pytest.raises(StateMismatchError):
            with manager.use(workspace_id=ws, agent_id=bob,
                             slug="stemlab", idempotency_key="read-while-cloning"):
                pass
        git(checkout, "status", "--porcelain")
        # Test-only local Git; the real callback MUST invoke the isolated Git Cog.
        subprocess.run(["git", "clone", "--no-local", "--no-hardlinks", "-q",
                        str(checkout), str(agent / "projects/stemlab")], check=True)
        git(agent / "projects/stemlab", "checkout", "-q", "--detach", plan["expected_commit"])

    result = manager.clone_for_edit(workspace_id=ws, agent_id=alice, slug="stemlab",
                                    agent_home=agent, idempotency_key="clone-execute",
                                    sandboxed_clone=admitted_clone)
    assert len(calls) == 1
    assert result["expected_commit"] == git(agent / "projects/stemlab", "rev-parse", "HEAD")
    assert (checkout / "build").exists() is False
    assert not ledger.active(ws)


def test_clone_failure_keeps_lease_indeterminate(store) -> None:
    manager, ledger, ws, alice, bob, home, checkout, _ = store
    agent = home / "alice"
    (agent / "projects").mkdir(parents=True)
    with pytest.raises(OSError):
        manager.clone_for_edit(workspace_id=ws, agent_id=alice, slug="stemlab",
                               agent_home=agent, idempotency_key="bad-clone",
                               sandboxed_clone=lambda plan: (_ for _ in ()).throw(OSError("lost")))
    assert ledger.active(ws)[0]["state"] == "indeterminate"
    with pytest.raises(StateMismatchError):
        manager.clean_for_edit(workspace_id=ws, agent_id=bob,
                               slug="stemlab", idempotency_key="blocked-until-reconciled")


def test_tool_environment_static_shared_vs_agent_runtime() -> None:
    from potbot.tools.shared_tool_store import tool_process_environment

    reader = tool_process_environment("stemlab", "alice")
    writer = tool_process_environment("stemlab", "alice", installing=True)
    assert reader["POTBOT_TOOL_CHECKOUT"] == "/usr/local/tools/stemlab/checkout"
    assert reader["VIRTUAL_ENV"] == "/usr/local/tools/stemlab/environment"
    assert reader["UV_CACHE_DIR"] == "/home/alice/.cache/tools/stemlab/uv"
    assert writer["UV_CACHE_DIR"] == "/usr/local/tools/stemlab/downloads/uv"
    assert reader["PYTHONDONTWRITEBYTECODE"] == "1"
    assert reader["HF_HOME"].startswith("/var/ai/")
    assert reader["TMPDIR"].startswith("/home/alice/")
    with pytest.raises(InvalidInputError):
        tool_process_environment("stemlab", "../unknown")


def test_clean_cannot_remove_tracked_files_even_when_manifest_mislabels_them(store) -> None:
    from dataclasses import replace

    manager, ledger, ws, alice, _, _, checkout, _ = store
    definition = manager.definition(ws, "stemlab")
    with manager.ledger._connect() as conn:
        import json

        tampered = replace(definition, cleanup_paths=("source.py",))
        conn.execute("UPDATE trusted_shared_tools SET definition_json=? WHERE workspace_id=? AND slug=?",
                     (json.dumps(tampered.to_dict()), str(ws), "stemlab"))
    with pytest.raises(ScopeDeniedError):
        manager.clean_for_edit(workspace_id=ws, agent_id=alice, slug="stemlab",
                               idempotency_key="tracked-clean")
    assert (checkout / "source.py").read_text() == "print('hello')\n"


def test_verified_reusable_binary_hash_detects_shared_tampering(store) -> None:
    from dataclasses import replace

    manager, _, ws, _, _, _, checkout, source = store
    binary = source / "static/ffmpeg"
    binary.write_bytes(b"version-1")
    approved = replace(
        manager.definition(ws, "stemlab"),
        reusable_artifacts=(("static/ffmpeg", hashlib.sha256(b"version-1").hexdigest()),),
    )
    assert manager._verify(approved) == checkout
    binary.write_bytes(b"version-2")
    with pytest.raises(StateMismatchError):
        manager._verify(approved)


def test_shared_tool_registration_without_probe_is_rejected(store) -> None:
    manager, _, ws, alice, _, _, _, source = store
    definition = manager.definition(ws, "stemlab")
    with pytest.raises(TypeError):
        manager.register(workspace_id=ws, agent_id=alice, definition=definition,
                         idempotency_key="bad-registration")


def test_normal_use_writing_to_shared_source_is_quarantined(store) -> None:
    manager, ledger, ws, alice, _, _, checkout, _ = store
    with pytest.raises(StateMismatchError):
        with manager.use(workspace_id=ws, agent_id=alice, slug="stemlab",
                         idempotency_key="unexpected-write"):
            (checkout / "generated.txt").write_text("should be agent local")
    assert ledger.active(ws)[0]["state"] == "indeterminate"
