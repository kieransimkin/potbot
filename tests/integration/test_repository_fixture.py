import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from potbot.domain import InvalidInputError, ScopeDeniedError
from potbot.evaluation.repository_fixture import (
    MARKER_NAME,
    FixtureVariant,
    check_repository_fixture,
    cleanup_repository_fixture,
    create_repository_fixture,
    load_repository_fixture,
    main,
    repair_repository_fixture,
)


@pytest.fixture
def repo(tmp_path: Path):
    fixture = create_repository_fixture(tmp_path)
    yield fixture
    if fixture.path.exists():
        cleanup_repository_fixture(fixture)


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_real_faulty_baseline_fails_and_documented_repair_passes(repo) -> None:
    failed = check_repository_fixture(repo)
    assert failed.returncode == 1
    assert "FAIL: test_total" in failed.stderr
    assert "FAIL: test_empty_total" in failed.stderr
    assert repair_repository_fixture(repo)
    passed = check_repository_fixture(repo)
    assert passed.returncode == 0, passed.stderr
    assert "Ran 2 tests" in passed.stderr
    assert not repair_repository_fixture(repo)


def test_sibling_commits_diverge_from_exact_baseline(repo) -> None:
    assert repo.left_commit != repo.right_commit != repo.baseline_commit
    assert git(repo.path, "rev-parse", "candidate-left^") == repo.baseline_commit
    assert git(repo.path, "rev-parse", "candidate-right^") == repo.baseline_commit
    assert git(repo.path, "merge-base", "candidate-left", "candidate-right") == repo.baseline_commit
    assert git(repo.path, "rev-parse", "HEAD") == repo.baseline_commit
    assert git(repo.path, "status", "--porcelain") == ""


def test_instances_have_independent_histories_and_cleanup_preserves_user_files(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "caller-owned-sentinel.txt"
    sentinel.write_text("user content must survive", encoding="utf-8")
    first = create_repository_fixture(tmp_path)
    second = create_repository_fixture(tmp_path)
    try:
        assert first.path != second.path
        assert first.baseline_commit != second.baseline_commit
        assert git(first.path, "rev-parse", "--git-dir") == ".git"
        repair_repository_fixture(first)
        assert check_repository_fixture(second).returncode == 1
        cleanup_repository_fixture(first)
        assert not first.path.exists()
        assert second.path.is_dir()
        assert sentinel.read_text(encoding="utf-8") == "user content must survive"
        assert tmp_path.is_dir()
    finally:
        if first.path.exists():
            cleanup_repository_fixture(first)
        cleanup_repository_fixture(second)


def test_dirty_tracked_change_is_real_and_preserved_by_repair(tmp_path: Path) -> None:
    fixture = create_repository_fixture(tmp_path, FixtureVariant.DIRTY_TRACKED)
    try:
        assert "M calculator.py" in git(fixture.path, "status", "--porcelain")
        assert "Deliberate local tracked change" in git(fixture.path, "diff")
        repair_repository_fixture(fixture)
        assert "Deliberate local tracked change" in (fixture.path / "calculator.py").read_text()
        assert check_repository_fixture(fixture).returncode == 0
    finally:
        cleanup_repository_fixture(fixture)


def test_untracked_input_changes_real_execution(tmp_path: Path) -> None:
    fixture = create_repository_fixture(tmp_path, FixtureVariant.UNTRACKED_INPUT)
    try:
        assert "?? runtime_offset.txt" in git(fixture.path, "status", "--porcelain")
        repair_repository_fixture(fixture)
        assert check_repository_fixture(fixture).returncode == 0
        (fixture.path / "runtime_offset.txt").write_text("9\n", encoding="utf-8")
        changed = check_repository_fixture(fixture)
        assert changed.returncode == 1
        assert "14 != 5" in changed.stderr
    finally:
        cleanup_repository_fixture(fixture)


def test_preexisting_failure_remains_after_targeted_repair(tmp_path: Path) -> None:
    fixture = create_repository_fixture(tmp_path, FixtureVariant.PRE_EXISTING_FAILURE)
    try:
        assert "failures=3" in check_repository_fixture(fixture).stderr
        repair_repository_fixture(fixture)
        after = check_repository_fixture(fixture)
        assert after.returncode == 1
        assert "intentional pre-existing fixture failure" in after.stderr
        assert "failures=1" in after.stderr
    finally:
        cleanup_repository_fixture(fixture)


def test_cleanup_rejects_wrong_token_missing_marker_and_caller_directory(
    repo, tmp_path: Path
) -> None:
    with pytest.raises(ScopeDeniedError):
        cleanup_repository_fixture(replace(repo, ownership_token="0" * 64))
    assert repo.path.exists()
    with pytest.raises(ScopeDeniedError):
        load_repository_fixture(tmp_path, repo.ownership_token)
    marker = repo.path / MARKER_NAME
    content = marker.read_bytes()
    marker.unlink()
    try:
        with pytest.raises(ScopeDeniedError):
            cleanup_repository_fixture(repo)
        assert (repo.path / "calculator.py").exists()
    finally:
        marker.write_bytes(content)


def test_cleanup_rejects_symlink_alias_and_preserves_external_symlink_target(
    repo, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched", encoding="utf-8")
    alias = tmp_path / "fixture-alias"
    try:
        alias.symlink_to(repo.path, target_is_directory=True)
        (repo.path / "external-link").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    with pytest.raises(ScopeDeniedError):
        load_repository_fixture(alias, repo.ownership_token)
    cleanup_repository_fixture(repo)
    assert outside.read_text(encoding="utf-8") == "untouched"


def test_repair_rejects_unrecognized_source(repo) -> None:
    (repo.path / "calculator.py").write_text("# changed by a developer\n", encoding="utf-8")
    with pytest.raises(InvalidInputError):
        repair_repository_fixture(repo)
    assert (repo.path / "calculator.py").read_text() == "# changed by a developer\n"


def test_interrupted_creation_keeps_recoverable_ownership(tmp_path: Path, monkeypatch) -> None:
    import potbot.evaluation.repository_fixture as module

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "_git", interrupt)
    with pytest.raises(KeyboardInterrupt):
        create_repository_fixture(tmp_path)
    children = list(tmp_path.glob("fixture-*"))
    assert len(children) == 1
    marker = json.loads((children[0] / MARKER_NAME).read_text())
    pending = load_repository_fixture(children[0], marker["ownership_token"])
    assert pending.baseline_commit is None
    cleanup_repository_fixture(pending)
    assert not children[0].exists()
    assert pending.sentinel_path.exists()


def test_git_environment_cannot_redirect_fixture_to_caller_repository(
    tmp_path: Path, monkeypatch
) -> None:
    redirected = tmp_path / "caller-git-dir"
    redirected.mkdir()
    keep = redirected / "keep.txt"
    keep.write_text("caller data", encoding="utf-8")
    monkeypatch.setenv("GIT_DIR", str(redirected))
    monkeypatch.setenv("GIT_WORK_TREE", str(redirected))
    fixture = create_repository_fixture(tmp_path / "fixtures")
    try:
        assert (fixture.path / ".git").is_dir()
        assert list(redirected.iterdir()) == [keep]
        assert keep.read_text() == "caller data"
    finally:
        cleanup_repository_fixture(fixture)


def test_cli_round_trip_has_real_exit_codes_and_cleanup(tmp_path: Path, capsys) -> None:
    assert main(["create", "--parent", str(tmp_path)]) == 0
    created = json.loads(capsys.readouterr().out)
    args = [created["path"], "--token", created["ownership_token"]]
    assert main(["check", *args]) == 1
    assert json.loads(capsys.readouterr().out)["returncode"] == 1
    assert main(["repair", *args]) == 0
    assert json.loads(capsys.readouterr().out)["changed"]
    assert main(["check", *args]) == 0
    assert json.loads(capsys.readouterr().out)["returncode"] == 0
    assert main(["cleanup", *args]) == 0
    capsys.readouterr()
    assert not Path(created["path"]).exists()
