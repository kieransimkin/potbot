"""POSIX immutable-release teardown, confinement and failure semantics."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from potbot.tools import temporary
from potbot.tools.temporary import TemporaryDirectory, TemporaryStorageError

pytestmark = pytest.mark.skipif(
    os.name != "posix" or not temporary._POSIX_FD_CLEANUP,
    reason="Requires POSIX descriptor-relative directory operations",
)


def _release(root: Path, mode: int = 0o555) -> Path:
    release = root / "skills" / "example" / "releases" / ("a" * 64)
    release.mkdir(parents=True)
    for name in ("release.json", "SKILL.md", "AGENTS.md", "MODEL.md", "MEMORY.md"):
        file = release / name
        file.write_text("immutable fixture\n", encoding="utf-8")
        file.chmod(0o444)
    release.chmod(mode)
    return release


@pytest.mark.parametrize("mode", [0o555, 0o500, 0o444, 0o400, 0o755, 0o700])
def test_readonly_release_tree_is_removed_only_at_cleanup(tmp_path: Path, mode: int) -> None:
    owner = TemporaryDirectory(dir=tmp_path)
    release = _release(owner.path, mode)
    assert stat.S_IMODE(release.stat().st_mode) == mode
    owner.cleanup()
    assert not owner.path.exists()
    owner.cleanup()  # Idempotent after successful removal.


def test_readonly_root_and_nested_directories_are_removed(tmp_path: Path) -> None:
    owner = TemporaryDirectory(dir=tmp_path)
    _release(owner.path)
    for directory in sorted(owner.path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if directory.is_dir():
            directory.chmod(0o555)
    owner.path.chmod(0o555)
    owner.cleanup()
    assert not owner.path.exists()


def test_only_owner_directory_bits_change_not_parent_or_file_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = TemporaryDirectory(dir=tmp_path)
    release = _release(owner.path, 0o550)
    parent_mode = stat.S_IMODE(tmp_path.stat().st_mode)
    calls: list[tuple[int, int]] = []
    original = os.fchmod

    def tracked(fd: int, mode: int) -> None:
        before = os.fstat(fd)
        assert stat.S_ISDIR(before.st_mode)
        assert before.st_uid == os.geteuid()
        assert stat.S_IMODE(before.st_mode) & 0o077 == mode & 0o077
        calls.append((before.st_ino, mode))
        original(fd, mode)

    inode = release.stat().st_ino
    with monkeypatch.context() as patch:
        patch.setattr(temporary.os, "fchmod", tracked)
        owner.cleanup()
    assert calls == [(inode, 0o750)]
    assert stat.S_IMODE(tmp_path.stat().st_mode) == parent_mode


def test_symlinks_and_external_hardlinks_do_not_mutate_targets(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.json"
    sentinel.write_text("outside bytes", encoding="utf-8")
    sentinel.chmod(0o444)
    outside.chmod(0o555)
    owner = TemporaryDirectory(dir=tmp_path)
    release = _release(owner.path)
    (owner.path / "directory-link").symlink_to(outside, target_is_directory=True)
    (owner.path / "file-link").symlink_to(sentinel)
    (owner.path / "dangling-link").symlink_to(outside / "absent")
    (owner.path / "loop-link").symlink_to(owner.path, target_is_directory=True)
    # POSIX unlink must not make a read-only file writable: hardlinks share mode.
    release.chmod(0o755)
    os.link(sentinel, release / "hardlink.json")
    release.chmod(0o555)
    owner.cleanup()
    assert not owner.path.exists()
    assert sentinel.read_text(encoding="utf-8") == "outside bytes"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o444
    assert stat.S_IMODE(outside.stat().st_mode) == 0o555
    assert sentinel.stat().st_nlink == 1


@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_replaced_root_is_rejected_before_chmod_or_deletion(
    tmp_path: Path, replacement: str
) -> None:
    owner = TemporaryDirectory(dir=tmp_path)
    original = owner.path.with_name(owner.path.name + "-original")
    owner.path.rename(original)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep"
    sentinel.write_text("outside", encoding="utf-8")
    if replacement == "symlink":
        owner.path.symlink_to(outside, target_is_directory=True)
    else:
        owner.path.mkdir()
        (owner.path / "keep").write_text("replacement", encoding="utf-8")
    try:
        with pytest.raises(TemporaryStorageError, match="retained"):
            owner.cleanup()
        assert not owner._cleaned
        assert sentinel.read_text(encoding="utf-8") == "outside"
        if replacement == "directory":
            assert (owner.path / "keep").read_text(encoding="utf-8") == "replacement"
    finally:
        if owner.path.is_symlink():
            owner.path.unlink()
        else:
            (owner.path / "keep").unlink()
            owner.path.rmdir()
        original.rename(owner.path)
        owner.cleanup()


def test_directory_to_symlink_swap_at_open_does_not_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = TemporaryDirectory(dir=tmp_path)
    victim = owner.path / "victim"
    victim.mkdir()
    victim.chmod(0o555)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep"
    sentinel.write_text("safe", encoding="utf-8")
    outside.chmod(0o555)
    displaced = owner.path / "displaced"
    original_open = os.open
    swapped = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "victim" and dir_fd is not None and not swapped:
            swapped = True
            victim.rename(displaced)
            victim.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr(temporary.os, "open", racing_open)
        with pytest.raises(TemporaryStorageError, match="retained"):
            owner.cleanup()
    assert swapped and not owner._cleaned
    assert sentinel.read_text(encoding="utf-8") == "safe"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o555
    owner.cleanup()


def test_fchmod_is_bound_to_opened_inode_when_path_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = TemporaryDirectory(dir=tmp_path)
    victim = owner.path / "victim"
    victim.mkdir()
    victim.chmod(0o555)
    inode = victim.stat().st_ino
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("safe", encoding="utf-8")
    outside.chmod(0o555)
    displaced = owner.path / "displaced"
    original = os.fchmod
    swapped = False

    def racing_chmod(fd: int, mode: int) -> None:
        nonlocal swapped
        if os.fstat(fd).st_ino == inode and not swapped:
            swapped = True
            victim.rename(displaced)
            victim.symlink_to(outside, target_is_directory=True)
        original(fd, mode)

    with monkeypatch.context() as patch:
        patch.setattr(temporary.os, "fchmod", racing_chmod)
        with pytest.raises(TemporaryStorageError, match="retained"):
            owner.cleanup()
    assert swapped
    assert stat.S_IMODE(outside.stat().st_mode) == 0o555
    assert (outside / "keep").read_text(encoding="utf-8") == "safe"
    assert stat.S_IMODE(displaced.stat().st_mode) == 0o755
    owner.cleanup()


def test_foreign_owner_directory_is_not_chmodded_even_by_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = TemporaryDirectory(dir=tmp_path)
    release = _release(owner.path)
    inode = release.stat().st_ino
    original = os.fstat

    def foreign_stat(fd: int):
        info = original(fd)
        if info.st_ino != inode:
            return info
        fields = list(info)
        fields[4] = info.st_uid + 1
        return os.stat_result(fields)

    with monkeypatch.context() as patch:
        patch.setattr(temporary.os, "fstat", foreign_stat)
        with pytest.raises(TemporaryStorageError, match="retained") as caught:
            owner.cleanup()
    assert isinstance(caught.value.__cause__, PermissionError)
    assert stat.S_IMODE(release.stat().st_mode) == 0o555
    assert (release / "release.json").exists()
    owner.cleanup()


def test_permanent_fchmod_denial_is_not_retried_or_silently_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = TemporaryDirectory(dir=tmp_path)
    release = _release(owner.path)
    calls = []

    def denied(fd: int, mode: int) -> None:
        calls.append((fd, mode))
        raise PermissionError("permanent chmod denial")

    with monkeypatch.context() as patch:
        patch.setattr(temporary.os, "fchmod", denied)
        with pytest.raises(TemporaryStorageError, match="retained") as caught:
            owner.cleanup()
    assert isinstance(caught.value.__cause__, PermissionError)
    assert len(calls) == 1 and not owner._cleaned
    assert stat.S_IMODE(release.stat().st_mode) == 0o555
    owner.cleanup()


def test_unreadable_directory_remains_an_explicit_denial(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("Root bypasses mode-bit denials; also run this suite unprivileged")
    owner = TemporaryDirectory(dir=tmp_path)
    unreadable = owner.path / "unreadable"
    unreadable.mkdir()
    unreadable.chmod(0)
    try:
        with pytest.raises(TemporaryStorageError, match="retained"):
            owner.cleanup()
        assert not owner._cleaned
        assert stat.S_IMODE(unreadable.stat().st_mode) == 0
    finally:
        unreadable.chmod(0o700)
        owner.cleanup()


def test_missing_descendant_and_missing_root_are_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = TemporaryDirectory(dir=tmp_path)
    (owner.path / "vanishes").write_text("fixture", encoding="utf-8")
    original = os.unlink

    def vanished(path, *, dir_fd=None):
        original(path, dir_fd=dir_fd)
        raise FileNotFoundError(path)

    with monkeypatch.context() as patch:
        patch.setattr(temporary.os, "unlink", vanished)
        owner.cleanup()
    assert not owner.path.exists()
    missing = TemporaryDirectory(dir=tmp_path)
    missing.path.rmdir()
    missing.cleanup()
    missing.cleanup()
    assert missing._cleaned


def test_parent_directory_is_not_repaired(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("Root bypasses mode-bit denials; also run this suite unprivileged")
    parent = tmp_path / "parent"
    parent.mkdir()
    owner = TemporaryDirectory(dir=parent)
    _release(owner.path)
    parent.chmod(0o555)
    try:
        with pytest.raises(TemporaryStorageError, match="retained"):
            owner.cleanup()
        assert stat.S_IMODE(parent.stat().st_mode) == 0o555
        assert owner.path.is_dir() and not owner._cleaned
    finally:
        parent.chmod(0o755)
        owner.cleanup()


def test_subprocess_pytest_exits_cleanly_after_readonly_fixture(tmp_path: Path) -> None:
    """Exercise config.add_cleanup(owner.cleanup), not just test assertions."""
    project = tmp_path / "probe"
    project.mkdir()
    root = Path(__file__).resolve().parents[2]
    shutil.copyfile(root / "conftest.py", project / "conftest.py")
    (project / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (project / "test_release.py").write_text(
        "def test_release(tmp_path):\n"
        "    release = tmp_path / 'release'\n"
        "    release.mkdir()\n"
        "    file = release / 'release.json'\n"
        "    file.write_text('{}', encoding='utf-8')\n"
        "    file.chmod(0o444)\n"
        "    release.chmod(0o555)\n",
        encoding="utf-8",
    )
    scratch = tmp_path / "child-scratch"
    scratch.mkdir()
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    env.update(
        PYTHONPATH=str(root / "src"),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        POTBOT_TEMP_ROOT=str(scratch),
        TMPDIR=str(scratch),
        TEMP=str(scratch),
        TMP=str(scratch),
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_release.py"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "1 passed" in output
    assert "TemporaryStorageError" not in output
    assert list(scratch.iterdir()) == []


def test_real_skill_store_stays_immutable_until_scratch_teardown(tmp_path: Path) -> None:
    from potbot.context.skills import SharedSkillReleaseStore

    text = "---\nname: cleanup-probe\ndescription: teardown regression\n---\n# Probe\n"
    outside = tmp_path / "external-store"
    outside.mkdir(mode=0o700)
    external_store = SharedSkillReleaseStore(outside)
    external_release = external_store.publish("cleanup-probe", text)
    owner = TemporaryDirectory(dir=tmp_path)
    store_root = owner.path / "store"
    store_root.mkdir(mode=0o700)
    store = SharedSkillReleaseStore(store_root)
    release = store.publish("cleanup-probe", text)
    assert store.publish("cleanup-probe", text) == release
    assert stat.S_IMODE(release.path.stat().st_mode) == 0o555
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o444 for p in release.path.iterdir())
    (owner.path / "external-link").symlink_to(outside, target_is_directory=True)
    owner.cleanup()
    assert not owner.path.exists()
    assert external_store.publish("cleanup-probe", text) == external_release
    assert stat.S_IMODE(external_release.path.stat().st_mode) == 0o555
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o444 for p in external_release.path.iterdir())


def test_ignored_cleanup_denial_warns_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = TemporaryDirectory(dir=tmp_path, ignore_cleanup_errors=True)
    release = _release(owner.path)

    def denied(fd: int, mode: int) -> None:
        raise PermissionError("permanent chmod denial")

    with monkeypatch.context() as patch:
        patch.setattr(temporary.os, "fchmod", denied)
        with pytest.warns(RuntimeWarning, match="retained"):
            owner.cleanup()
    assert not owner._cleaned and (release / "release.json").exists()
    owner.cleanup()
    assert not owner.path.exists()


def test_directory_descriptors_close_after_success_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptors = Path("/proc/self/fd")
    if not descriptors.is_dir():
        pytest.skip("Linux descriptor accounting probe")
    before = len(list(descriptors.iterdir()))

    def denied(fd: int, mode: int) -> None:
        raise PermissionError("permanent chmod denial")

    for _ in range(20):
        owner = TemporaryDirectory(dir=tmp_path)
        _release(owner.path)
        with monkeypatch.context() as patch:
            patch.setattr(temporary.os, "fchmod", denied)
            with pytest.raises(TemporaryStorageError):
                owner.cleanup()
        owner.cleanup()
    assert len(list(descriptors.iterdir())) == before
