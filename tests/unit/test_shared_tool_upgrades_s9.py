"""S9 offline real-Git test/release switching; NOT live Bubblewrap qualification."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools.agent_homes import AgentHomeRegistry
from potbot.tools.shared_tool_store import SharedToolDefinition, SharedToolStore
from potbot.tools.shared_tool_upgrades import (
    SharedToolUpgradeManager,
    ToolTestCase,
    ToolTestSuite,
    stage_name,
)
from potbot.tools.shared_workspace_isolation import SharedWorkspaceRuntime
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger


def git(cwd: Path, *argv: str) -> str:
    run = subprocess.run(["git", *argv], cwd=cwd, capture_output=True, check=True)
    return run.stdout.decode().strip()


def install(parent: Path, version: str) -> SharedToolDefinition:
    for name in ("checkout", "environment", "static", "downloads"):
        (parent / name).mkdir(parents=True, exist_ok=True)
    repo = parent / "checkout"
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.org")
    git(repo, "config", "user.name", "Example")
    (repo / "main.py").write_text(f"print({version!r})\n")
    (repo / "requirements.lock").write_text(f"version={version}\n")
    (repo / ".gitignore").write_text("build/\n.pytest_cache/\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", version)
    git(repo, "remote", "add", "origin", "https://github.com/kieransimkin/stemlab")
    app = parent / "static" / "stemlab"
    app.write_bytes(version.encode())
    app.chmod(0o755)
    return SharedToolDefinition(
        "stemlab", "https://github.com/kieransimkin/stemlab", git(repo, "rev-parse", "HEAD"),
        ("build", ".pytest_cache"),
        (("requirements.lock", hashlib.sha256((repo / "requirements.lock").read_bytes()).hexdigest()),),
        False,
        (("static/stemlab", hashlib.sha256(app.read_bytes()).hexdigest()),),
    )


class FixtureTestExecutor:
    def __init__(self):
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.fail = False

    def run(self, *, case: ToolTestCase, argv: tuple[str, ...], workspace_id: UUID,
            agent_id: UUID, agent_dir: Path, uid: int, gid: int, cancelled):
        assert not cancelled()
        assert uid == 420 and gid == 69
        assert argv[0] == "/usr/bin/python3"
        assert argv[1] == "-c"
        assert argv[2] == "pass"
        assert argv[3].startswith("/usr/local/tools/.potbot-s9-stage-stemlab-")
        assert agent_dir.is_dir()
        self.calls.append((case.name, argv))
        return dict(name=case.name, kind=case.kind, passed=not self.fail,
                    outcome="completed" if not self.fail else "nonzero_exit",
                    exit_code=0 if not self.fail else 1, elapsed_ms=32,
                    dispatched=True, stopped=True, isolation_identity="unit-fixture-only",
                    output_sha256=hashlib.sha256(case.name.encode()).hexdigest())


@pytest.fixture
def rig(tmp_path: Path):
    home = tmp_path / "workspaces" / "home"
    home.mkdir(parents=True)
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    tools = tmp_path / "shared-tools"
    tools.mkdir()
    private = tmp_path / "operator"
    private.mkdir()
    workspace_id, agent_id, operation_id = uuid4(), uuid4(), uuid4()
    ledger = WorkspaceLeaseLedger(private / "ledger.sqlite", workspace_home=home)
    ledger.pin_image(
        workspace_id, digest="a" * 64,
        image_ref="ghcr.io/test/root@sha256:" + "a" * 64,
        build_commit="c" * 40, platform="linux/amd64",
        rootfs_path=rootfs, qualification_sha256="b" * 64,
    )
    store = SharedToolStore(ledger=ledger, shared_root=tools,
                            authorize=lambda w, a: (w, a) == (workspace_id, agent_id))
    old = install(tools / "stemlab", "v1")
    new = install(tools / stage_name("stemlab", operation_id), "v2")
    with ledger._connect() as db:
        db.execute("INSERT INTO trusted_shared_tools VALUES(?,?,?)",
                   (str(workspace_id), "stemlab", json.dumps(old.to_dict(), sort_keys=True)))
    homes = AgentHomeRegistry(ledger, workspace_name="studio")
    homes.provision(workspace_id, agent_id)
    runtime = SharedWorkspaceRuntime("a" * 64, rootfs, home, "b" * 64, shared_tools=tools)
    executor = FixtureTestExecutor()
    manager = SharedToolUpgradeManager(
        store=store, runtime=runtime, homes=homes,
        authorize=lambda w, a: (w, a) == (workspace_id, agent_id), executor=executor,
    )
    suite = ToolTestSuite((
        ToolTestCase("startup", "smoke", ("/usr/bin/python3", "-c", "pass", "{checkout}")),
        ToolTestCase("regression", "unit", ("/usr/bin/python3", "-c", "pass", "{checkout}")),
    ))
    return locals()


def prepare(rig):
    return rig["manager"].prepare(
        workspace_id=rig["workspace_id"], agent_id=rig["agent_id"],
        operation_id=rig["operation_id"], expected_old_commit=rig["old"].commit,
        replacement=rig["new"], suite=rig["suite"],
    )


def activate(rig, *, confirm=True):
    return rig["manager"].activate(
        workspace_id=rig["workspace_id"], agent_id=rig["agent_id"],
        operation_id=rig["operation_id"], slug="stemlab",
        expected_old_commit=rig["old"].commit, confirm=confirm,
    )


def rollback(rig, *, confirm=True, current=None):
    return rig["manager"].rollback(
        workspace_id=rig["workspace_id"], agent_id=rig["agent_id"],
        operation_id=rig["operation_id"], slug="stemlab",
        expected_current_commit=current or rig["new"].commit, confirm=confirm,
    )


def test_real_git_stage_tests_and_clean_probe(rig):
    prepared = prepare(rig)
    assert prepared["state"] == "ready"
    assert prepared["passed_tests"] == 2
    assert len(rig["executor"].calls) == 2
    assert rig["store"].definition(rig["workspace_id"], "stemlab").commit == rig["old"].commit
    assert git(rig["tools"] / stage_name("stemlab", rig["operation_id"]) / "checkout",
               "status", "--porcelain") == ""
    assert not (rig["tools"] / "stemlab" / "checkout" / "build").exists()
    again = rig["manager"].inspect(workspace_id=rig["workspace_id"],
                                    agent_id=rig["agent_id"],operation_id=rig["operation_id"])
    assert again["state"] == "ready" and len(again["test_results"]) == 2
    with pytest.raises(StateMismatchError):
        prepare(rig)


def test_atomic_switch_and_rollback_preserve_previous_release(rig):
    prepare(rig)
    active = activate(rig)
    assert active["state"] == "active"
    assert rig["store"].definition(rig["workspace_id"], "stemlab").commit == rig["new"].commit
    assert (rig["tools"] / "stemlab/static/stemlab").read_bytes() == b"v2"
    stage = rig["tools"] / stage_name("stemlab", rig["operation_id"])
    assert (stage / "static/stemlab").read_bytes() == b"v1"
    planned = rig["manager"].runtime_entrypoint_updates(
        workspace_id=rig["workspace_id"], agent_id=rig["agent_id"],
        operation_id=rig["operation_id"])
    assert planned["pins"]["/usr/local/tools/stemlab/static/stemlab"] == hashlib.sha256(b"v2").hexdigest()
    assert planned["requires_operator_runtime_repin"] is True
    final = rollback(rig)
    assert final["state"] == "rolled_back"
    assert rig["store"].definition(rig["workspace_id"], "stemlab").commit == rig["old"].commit
    assert (rig["tools"] / "stemlab/static/stemlab").read_bytes() == b"v1"
    assert (stage / "static/stemlab").read_bytes() == b"v2"


def test_reject_unauthorized_and_wrong_previous_commit(rig):
    bad = rig.copy()
    bad["agent_id"] = uuid4()
    with pytest.raises(ScopeDeniedError):
        prepare(bad)
    with pytest.raises(StateMismatchError):
        rig["manager"].prepare(
            workspace_id=rig["workspace_id"], agent_id=rig["agent_id"],
            operation_id=rig["operation_id"], expected_old_commit="0" * 40,
            replacement=rig["new"], suite=rig["suite"],
        )


def test_test_failure_never_activates_candidate(rig):
    rig["executor"].fail = True
    with pytest.raises(StateMismatchError):
        prepare(rig)
    assert rig["store"].definition(rig["workspace_id"], "stemlab").commit == rig["old"].commit
    assert (rig["tools"] / "stemlab/static/stemlab").read_bytes() == b"v1"


def test_dirty_stage_and_bad_artifact_are_denied(rig):
    stage = rig["tools"] / stage_name("stemlab", rig["operation_id"])
    (stage / "checkout" / "unknown.txt").write_text("unknown")
    with pytest.raises(StateMismatchError):
        prepare(rig)
    (stage / "checkout" / "unknown.txt").unlink()
    (stage / "static/stemlab").write_bytes(b"tampered")
    with pytest.raises(StateMismatchError):
        prepare(rig)


def test_activation_needs_explicit_approval_and_no_stage_drift(rig):
    prepare(rig)
    with pytest.raises(ScopeDeniedError):
        activate(rig, confirm=False)
    stage = rig["tools"] / stage_name("stemlab", rig["operation_id"])
    (stage / "static/stemlab").write_bytes(b"tampered")
    with pytest.raises(StateMismatchError):
        activate(rig)
    assert (rig["tools"] / "stemlab/static/stemlab").read_bytes() == b"v1"


def test_atomic_activation_rejects_active_shared_tool_reader(rig):
    prepare(rig)
    # Simulate a still-live brokered reader holding the named tool resource.
    rig["ledger"].active_guards[(rig["workspace_id"], "stemlab")] = object()
    with pytest.raises(StateMismatchError):
        activate(rig)
    rig["ledger"].active_guards.clear()
    assert (rig["tools"] / "stemlab/static/stemlab").read_bytes() == b"v1"


def test_interrupted_activation_requires_explicit_recovery(rig, monkeypatch):
    prepare(rig)
    original = rig["manager"]._commit_swap
    monkeypatch.setattr(rig["manager"], "_commit_swap",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("simulated DB outage")))
    with pytest.raises(OSError):
        activate(rig)
    with rig["ledger"]._connect() as db:
        row = db.execute("SELECT state FROM trusted_tool_upgrades WHERE operation_id=?",
                         (str(rig["operation_id"]),)).fetchone()
        assert row["state"] == "switching"
    assert (rig["tools"] / "stemlab/static/stemlab").read_bytes() == b"v2"
    with pytest.raises(StateMismatchError):
        rig["store"].definition(rig["workspace_id"], "stemlab")
    # An indeterminate lease blocks all other activity until host reconciliation.
    with pytest.raises(StateMismatchError):
        rig["manager"].reconcile(workspace_id=rig["workspace_id"],agent_id=rig["agent_id"],
                                  operation_id=rig["operation_id"],slug="stemlab",confirm=True)
    rig["ledger"].active_guards.clear()  # Test-only stand-in for host lease reconciliation.
    monkeypatch.setattr(rig["manager"], "_commit_swap", original)
    recovered = rig["manager"].reconcile(
        workspace_id=rig["workspace_id"],agent_id=rig["agent_id"],
        operation_id=rig["operation_id"],slug="stemlab",confirm=True)
    assert recovered["state"] == "aborted"
    assert (rig["tools"] / "stemlab/static/stemlab").read_bytes() == b"v1"


def test_interrupted_rollback_reconciles_db(rig, monkeypatch):
    prepare(rig); activate(rig)
    original = rig["manager"]._commit_swap
    monkeypatch.setattr(rig["manager"], "_commit_swap",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("simulated outage")))
    with pytest.raises(OSError):
        rollback(rig)
    rig["ledger"].active_guards.clear()
    with pytest.raises(StateMismatchError):
        rig["store"].definition(rig["workspace_id"], "stemlab")
    monkeypatch.setattr(rig["manager"], "_commit_swap", original)
    final = rig["manager"].reconcile(
        workspace_id=rig["workspace_id"],agent_id=rig["agent_id"],
        operation_id=rig["operation_id"],slug="stemlab",confirm=True)
    assert final["state"] == "rolled_back"
    assert rig["store"].definition(rig["workspace_id"],"stemlab").commit == rig["old"].commit


def test_rollback_cannot_revert_an_unrelated_newer_pin(rig):
    prepare(rig); activate(rig)
    with pytest.raises(StateMismatchError):
        rollback(rig, current="0" * 40)
    assert rig["store"].definition(rig["workspace_id"],"stemlab").commit == rig["new"].commit


def test_missing_unit_or_smoke_suite_and_bad_stage_identity(rig):
    case = ToolTestCase("startup", "smoke", ("/usr/bin/python3", "-c", "pass"))
    with pytest.raises(InvalidInputError):
        ToolTestSuite((case, case))
    with pytest.raises(InvalidInputError):
        stage_name("../escape",uuid4())
    with pytest.raises(InvalidInputError):
        ToolTestCase("bad", "smoke", ("/home/private.sh",))


def test_unpinned_runtime_asset_drift_blocks_promotion(rig):
    stage = rig["tools"] / stage_name("stemlab", rig["operation_id"])
    (stage / "environment" / "runtime.cfg").write_text("compiler=original\n")
    prepare(rig)
    (stage / "environment" / "runtime.cfg").write_text("compiler=tampered\n")
    with pytest.raises(StateMismatchError):
        activate(rig)
    assert (rig["tools"] / "stemlab/static/stemlab").read_bytes() == b"v1"


def test_hardlinked_runtime_assets_fail_closed(rig):
    stage = rig["tools"] / stage_name("stemlab", rig["operation_id"])
    source = stage / "downloads" / "archive.whl"
    source.write_bytes(b"package")
    extra = rig["tools"] / "stemlab" / "downloads" / "shared.whl"
    extra.hardlink_to(source)
    with pytest.raises(ScopeDeniedError):
        prepare(rig)


def test_missing_staged_checkout_and_symlink_destination_rejected(rig):
    stage = rig["tools"] / stage_name("stemlab", rig["operation_id"])
    linked = stage / "downloads"
    linked.rmdir()
    linked.symlink_to(rig["tools"] / "stemlab/downloads")
    with pytest.raises(ScopeDeniedError):
        prepare(rig)
