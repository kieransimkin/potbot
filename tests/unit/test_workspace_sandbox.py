from __future__ import annotations

import sys
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.persistence.schema import application_migrations
from potbot.tools.supervisor import ProcessRequest
from potbot.workspace_sandbox import WorkspaceSandbox


def test_prepare_creates_fixed_layout_and_runtime_environment(tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox.prepare(tmp_path / "workspace")
    for area in (
        "repository",
        "candidates",
        "models",
        "tools",
        "dependencies",
        "environments",
        "builds",
        "tests",
        "artifacts",
        "receipts",
        "cache",
        "tmp",
        "logs",
    ):
        assert sandbox.path(area).is_dir()
    for value in sandbox.runtime_environment().values():
        if value == str(sandbox.root):
            continue
        assert Path(value).resolve().is_relative_to(sandbox.root)


def test_root_and_managed_paths_fail_closed_on_escape_or_links(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError):
        WorkspaceSandbox.prepare(Path("relative"))
    sandbox = WorkspaceSandbox.prepare(tmp_path / "workspace")
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ScopeDeniedError):
        sandbox.confine(outside)
    link = sandbox.path("repository") / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("host cannot create directory symlinks")
    with pytest.raises(ScopeDeniedError):
        sandbox.confine(link, area="repository", must_exist=True)


def test_workspace_model_path_cannot_escape_selected_root(tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox.prepare(tmp_path / "workspace")
    model = sandbox.path("models") / "model"
    assert sandbox.confine(model, area="models") == model
    with pytest.raises(ScopeDeniedError):
        sandbox.confine(tmp_path / "global-model", area="models")


def test_process_request_rejects_writable_mount_outside_sandbox(tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox.prepare(tmp_path / "workspace")
    candidate = sandbox.path("candidates") / "candidate"
    candidate.mkdir()
    executable = Path(sys.executable).resolve()
    ProcessRequest(
        (str(executable), "-c", "print('ok')"),
        candidate,
        candidate,
        sandbox_root=sandbox.root,
        writable_roots=(sandbox.path("tmp"),),
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(InvalidInputError):
        ProcessRequest(
            (str(executable), "-c", "print('no')"),
            candidate,
            candidate,
            sandbox_root=sandbox.root,
            writable_roots=(outside,),
        )


def test_workspace_sandbox_is_migration_14() -> None:
    migrations = application_migrations()
    # Migration 14 is immutable; newer features append migrations 15 onward.
    assert tuple(item.version for item in migrations) == tuple(range(1, len(migrations) + 1))
    sandbox_migration = migrations[13]
    assert sandbox_migration.version == 14
    assert sandbox_migration.name == "workspace_sandbox_schema"
    assert sandbox_migration.workspace_tables == ("workspace_sandboxes",)
    assert sandbox_migration.append_only_tables == ("workspace_sandboxes",)
