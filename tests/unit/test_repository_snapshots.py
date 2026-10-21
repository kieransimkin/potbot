"""Snapshot behavior on real temporary Git repositories, without a database."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, StateMismatchError
from potbot.domain.serialization import canonical_json
from potbot.evaluation.repository_fixture import (
    FixtureVariant,
    create_repository_fixture,
    repair_repository_fixture,
)
from potbot.tools.repository import (
    RepositorySnapshot,
    SnapshotProfile,
    capture_snapshot,
    materialize_snapshot,
    workspace_fingerprint,
)


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def behavior(path: Path) -> tuple[int, bool]:
    result = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
        cwd=path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode, "failures=" in result.stderr


def test_committed_capture_uses_object_ids_when_branch_labels_move(tmp_path: Path) -> None:
    fixture = create_repository_fixture(tmp_path / "source")
    left = capture_snapshot(fixture.path, committed_ref="candidate-left")
    right = capture_snapshot(fixture.path, committed_ref="candidate-right")
    assert left.manifest["commit_id"] == fixture.left_commit
    assert right.manifest["commit_id"] == fixture.right_commit
    assert left.content_digest != right.content_digest
    git(fixture.path, "branch", "-f", "candidate-left", "candidate-right")
    assert left.manifest["commit_id"] == fixture.left_commit
    materialize_snapshot(left, tmp_path / "left")
    assert (tmp_path / "left" / "branch_note.txt").read_text() == "left branch observation\n"


def test_dirty_and_untracked_runtime_inputs_roundtrip_without_source_changes(
    tmp_path: Path,
) -> None:
    fixture = create_repository_fixture(tmp_path / "source", FixtureVariant.DIRTY_TRACKED)
    repair_repository_fixture(fixture)
    (fixture.path / "runtime_offset.txt").write_text("0\n")
    (fixture.path / "private.env").write_text("SECRET_CANARY_DO_NOT_COPY")
    before = git(fixture.path, "status", "--porcelain=v1", "--ignored")
    profile = SnapshotProfile(
        untracked_paths=("runtime_offset.txt", "private.env"),
        excluded_paths=("private.env",),
        secret_refs=("secret://fixture/private",),
    )
    snapshot = capture_snapshot(fixture.path, profile=profile)
    assert b"SECRET_CANARY" not in snapshot.payload
    assert snapshot.profile.secret_refs == ("secret://fixture/private",)
    target = tmp_path / "materialized"
    materialize_snapshot(snapshot, target)
    assert behavior(target) == behavior(fixture.path) == (0, False)
    assert workspace_fingerprint(target) == snapshot.content_digest
    assert git(fixture.path, "status", "--porcelain=v1", "--ignored") == before
    assert not (target / "private.env").exists()
    (fixture.path / "runtime_offset.txt").write_text("7\n")
    changed = capture_snapshot(fixture.path, profile=profile)
    assert changed.content_digest != snapshot.content_digest
    materialize_snapshot(changed, tmp_path / "changed")
    assert behavior(tmp_path / "changed") == (1, True)


def test_staged_new_file_deletion_generated_input_and_executable_mode(tmp_path: Path) -> None:
    fixture = create_repository_fixture(tmp_path / "source")
    (fixture.path / "generated").mkdir()
    (fixture.path / "generated" / "offset.bin").write_bytes(b"\x00\xff")
    (fixture.path / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    (fixture.path / "run.sh").chmod(0o755)
    git(fixture.path, "add", "run.sh")
    (fixture.path / "calculator.py").unlink()
    snapshot = capture_snapshot(
        fixture.path, profile=SnapshotProfile(untracked_paths=("generated",))
    )
    target = tmp_path / "out"
    materialize_snapshot(snapshot, target)
    assert not (target / "calculator.py").exists()
    assert (target / "generated" / "offset.bin").read_bytes() == b"\x00\xff"
    assert (target / "run.sh").stat().st_mode & 0o111


def test_declared_missing_input_and_escaped_symlinks_are_not_silently_omitted(
    tmp_path: Path,
) -> None:
    fixture = create_repository_fixture(tmp_path / "source")
    with pytest.raises(CapabilityUnavailableError):
        capture_snapshot(fixture.path, profile=SnapshotProfile(untracked_paths=("missing.dat",)))
    (fixture.path / "outside").symlink_to(tmp_path / "secret")
    with pytest.raises(CapabilityUnavailableError):
        capture_snapshot(fixture.path, profile=SnapshotProfile(untracked_paths=("outside",)))


def test_internal_symlink_roundtrip_and_destination_ownership(tmp_path: Path) -> None:
    fixture = create_repository_fixture(tmp_path / "source")
    (fixture.path / "alias.py").symlink_to("calculator.py")
    snapshot = capture_snapshot(
        fixture.path, profile=SnapshotProfile(untracked_paths=("alias.py",))
    )
    materialize_snapshot(snapshot, tmp_path / "out")
    assert (tmp_path / "out" / "alias.py").is_symlink()
    assert (tmp_path / "out" / "alias.py").read_bytes() == (
        fixture.path / "calculator.py"
    ).read_bytes()
    with pytest.raises(FileExistsError):
        materialize_snapshot(snapshot, tmp_path / "out")
    link = tmp_path / "aliasdir"
    link.symlink_to(tmp_path / "out", target_is_directory=True)
    with pytest.raises(InvalidInputError):
        materialize_snapshot(snapshot, link / "child")


def test_snapshot_corruption_duplicate_entries_and_traversal_rejected(tmp_path: Path) -> None:
    fixture = create_repository_fixture(tmp_path / "source")
    snapshot = capture_snapshot(fixture.path)
    value = json.loads(snapshot.payload)
    value["files"][0]["data"] = "Y29ycnVwdA=="
    with pytest.raises(InvalidInputError):
        RepositorySnapshot(canonical_json(value).encode())
    with pytest.raises(InvalidInputError):
        RepositorySnapshot(snapshot.payload + b"\n")
    for path in ("../escape", "/absolute", ".git/config", "x/./y", "x\\y"):
        with pytest.raises(InvalidInputError):
            SnapshotProfile(untracked_paths=(path,))


def test_git_environment_and_fsmonitor_cannot_redirect_or_execute(
    tmp_path: Path, monkeypatch
) -> None:
    fixture = create_repository_fixture(tmp_path / "source")
    hook = tmp_path / "untrusted-hook"
    sentinel = tmp_path / "hook-ran"
    hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n")
    hook.chmod(0o755)
    git(fixture.path, "config", "core.fsmonitor", str(hook))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nonexistent"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    snapshot = capture_snapshot(fixture.path)
    assert snapshot.manifest["commit_id"] == fixture.baseline_commit
    assert not sentinel.exists()


def test_changed_file_during_capture_fails_instead_of_publishing_mixed_state(
    tmp_path: Path, monkeypatch
) -> None:
    import potbot.tools.repository_snapshot as module

    fixture = create_repository_fixture(tmp_path / "source")
    original = module._read_entry
    calls = 0

    def racing(root: Path, relative: str):
        nonlocal calls
        result = original(root, relative)
        if relative == "calculator.py":
            calls += 1
            if calls == 1:
                (root / relative).write_text("# changed concurrently\n")
        return result

    monkeypatch.setattr(module, "_read_entry", racing)
    with pytest.raises(StateMismatchError):
        capture_snapshot(fixture.path)


def test_gitlink_references_are_preserved_but_execution_is_unavailable(tmp_path: Path) -> None:
    fixture = create_repository_fixture(tmp_path / "source")
    git(
        fixture.path,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{fixture.left_commit},dependency",
    )
    git(fixture.path, "commit", "-m", "Add explicit gitlink")
    snapshot = capture_snapshot(fixture.path, committed_ref="HEAD")
    assert snapshot.manifest["submodules"] == {"dependency": fixture.left_commit}
    assert snapshot.manifest["reproducible"] is False
    with pytest.raises(CapabilityUnavailableError):
        materialize_snapshot(snapshot, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_snapshot_profile_does_not_inherit_unlisted_ignored_files(tmp_path: Path) -> None:
    fixture = create_repository_fixture(tmp_path / "source")
    (fixture.path / "__pycache__").mkdir()
    (fixture.path / "__pycache__" / "credential.pyc").write_bytes(b"HIDDEN_CANARY")
    captured = capture_snapshot(fixture.path)
    assert b"HIDDEN_CANARY" not in captured.payload
    materialize_snapshot(captured, tmp_path / "out")
    assert not (tmp_path / "out" / "__pycache__").exists()
    assert os.path.exists(fixture.path / "__pycache__" / "credential.pyc")


def test_git_inspection_output_limit_is_enforced_while_reading(tmp_path: Path) -> None:
    from potbot.tools.repository_snapshot import _git

    fixture = create_repository_fixture(tmp_path / "source")
    with pytest.raises(InvalidInputError, match="output limit"):
        _git(fixture.path, "ls-files", "-z", max_output=4)


def test_candidate_git_control_data_is_not_silently_dropped(tmp_path: Path) -> None:
    from potbot.tools.repository_snapshot import capture_candidate

    fixture = create_repository_fixture(tmp_path / "source")
    snapshot = capture_snapshot(fixture.path)
    target = tmp_path / "out"
    materialize_snapshot(snapshot, target)
    (target / ".git").mkdir()
    (target / ".git" / "config").write_text("[core]\n  hooksPath = external\n")
    with pytest.raises(CapabilityUnavailableError, match="Git control"):
        capture_candidate(target, snapshot)
