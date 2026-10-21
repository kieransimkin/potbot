"""Coding envelopes and the bounded text patch parser, including adversarial paths."""

import base64
import os
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.domain.coding import ScriptedCodingResponse
from potbot.domain.errors import InvalidInputError
from potbot.tools.patch_worker import PatchRejected, apply_payload


def payload(patch: bytes, *scope: str) -> dict:
    return {
        "schema_version": 1,
        "patch_base64": base64.b64encode(patch).decode(),
        "permitted_paths": list(scope),
    }


def edit(path: str, *, old_path: str | None = None) -> bytes:
    return f"diff --git a/{old_path or path} b/{path}\n--- a/{old_path or path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new\n".encode()


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"patch": b"x", "inability_code": "unsupported", "inability_reason": "No"},
        {"patch": b"x", "inability_reason": "No"},
        {"patch": b"\x00"},
        {"patch": b"\xff"},
        {"inability_code": "invented", "inability_reason": "No"},
    ],
)
def test_response_is_exactly_patch_or_classified_inability(fields: dict) -> None:
    with pytest.raises(InvalidInputError):
        ScriptedCodingResponse(uuid4(), uuid4(), **fields)


def test_text_edit_add_and_delete_apply_as_one_patch(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("old\n")
    (tmp_path / "src" / "gone.py").write_text("remove\n")
    patch = (
        edit("src/a.py")
        + b"""diff --git a/src/new.py b/src/new.py
new file mode 100644
--- /dev/null
+++ b/src/new.py
@@ -0,0 +1 @@
+added
diff --git a/src/gone.py b/src/gone.py
deleted file mode 100644
--- a/src/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-remove
"""
    )
    assert set(apply_payload(payload(patch, "src"), tmp_path)) == {
        "src/a.py",
        "src/new.py",
        "src/gone.py",
    }
    assert (tmp_path / "src" / "a.py").read_text() == "new\n"
    assert (tmp_path / "src" / "new.py").read_text() == "added\n"
    assert not (tmp_path / "src" / "gone.py").exists()


@pytest.mark.parametrize("path", ["outside.py", "src2/a.py", "../escape.py", ".git/config"])
def test_scope_uses_directory_boundaries_and_rejects_administrative_paths(
    tmp_path: Path, path: str
) -> None:
    (tmp_path / "src").mkdir()
    with pytest.raises(PatchRejected):
        apply_payload(payload(edit(path), "src"), tmp_path)
    assert list((tmp_path / "src").iterdir()) == []


@pytest.mark.parametrize(
    "patch",
    [
        b"diff --git a/outside b/src/new\nsimilarity index 100%\nrename from outside\nrename to src/new\n",
        b"diff --git a/src/old b/outside\nsimilarity index 100%\nrename from src/old\nrename to outside\n",
        edit("src/new", old_path="outside"),
    ],
)
def test_both_source_and_destination_paths_are_checked(tmp_path: Path, patch: bytes) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "outside").write_text("old\n")
    (tmp_path / "src" / "old").write_text("old\n")
    (tmp_path / "src" / "new").write_text("old\n")
    with pytest.raises(PatchRejected):
        apply_payload(payload(patch, "src"), tmp_path)
    assert (tmp_path / "outside").read_text() == "old\n"
    assert (tmp_path / "src" / "new").read_text() == "old\n"


def test_deleting_outside_scope_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "outside").write_text("old\n")
    patch = b"diff --git a/outside b/outside\ndeleted file mode 100644\n--- a/outside\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n"
    with pytest.raises(PatchRejected):
        apply_payload(payload(patch, "src"), tmp_path)
    assert (tmp_path / "outside").exists()


@pytest.mark.parametrize("kind", ["symlink", "directory_symlink", "fifo", "git_config"])
def test_links_special_files_and_local_git_configuration_are_rejected(
    tmp_path: Path, kind: str
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.py").write_text("old\n")
    target = "src/a.py"
    if kind == "directory_symlink":
        (candidate / "src").symlink_to(outside, target_is_directory=True)
    else:
        (candidate / "src").mkdir()
        if kind == "symlink":
            (candidate / target).symlink_to(outside / "a.py")
        elif kind == "fifo":
            os.mkfifo(candidate / target)
        else:
            (candidate / target).write_text("old\n")
            (candidate / ".git").mkdir()
            (candidate / ".git" / "config").write_text("[core]\nfsmonitor = malicious-command\n")
    with pytest.raises(PatchRejected):
        apply_payload(payload(edit(target), "src"), candidate)
    assert (outside / "a.py").read_text() == "old\n"


def test_patch_cannot_create_symlinks_or_partial_mutations(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("old\n")
    patch = (
        edit("a.py")
        + b"diff --git a/link b/link\nnew file mode 120000\n--- /dev/null\n+++ b/link\n@@ -0,0 +1 @@\n+../outside\n"
    )
    with pytest.raises(PatchRejected):
        apply_payload(payload(patch, "a.py", "link"), tmp_path)
    assert (tmp_path / "a.py").read_text() == "old\n"
    assert not (tmp_path / "link").is_symlink()


def test_git_quoted_path_with_spaces_applies(tmp_path: Path) -> None:
    (tmp_path / "with space.py").write_text("old\n")
    patch = b'diff --git "a/with space.py" "b/with space.py"\n--- "a/with space.py"\n+++ "b/with space.py"\n@@ -1 +1 @@\n-old\n+new\n'
    assert apply_payload(payload(patch, "with space.py"), tmp_path) == ["with space.py"]
    assert (tmp_path / "with space.py").read_text() == "new\n"
