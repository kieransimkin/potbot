"""Actual file lifecycle and fault injection for shared Windows/POSIX scratch."""

import ast
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from potbot.tools import temporary
from potbot.tools.temporary import TemporaryDirectory, TemporaryStorageError, scratch_root


def test_all_file_types_unicode_child_reopen_and_complete_cleanup(tmp_path):
    owner = TemporaryDirectory(dir=tmp_path)
    with owner as name:
        work = Path(name)
        child = work / "nested"
        child.mkdir()
        payload = bytes(range(256)) * 8
        for suffix in ("bin", "json", "log", "png", "wav", "py"):
            path = child / f"café-日本語.{suffix}"
            path.write_bytes(payload)
            assert path.read_bytes() == payload
        reopened = child / "closed-before-child.bin"
        reopened.write_bytes(payload)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; assert len(Path(sys.argv[1]).read_bytes()) == 2048",
                str(reopened),
            ],
            capture_output=True,
            timeout=10,
            check=True,
        )
        assert not result.stderr
        readonly = child / "readonly.bin"
        readonly.write_bytes(payload)
        readonly.chmod(stat.S_IREAD)
    assert not owner.path.exists()
    owner.cleanup()


def test_each_run_and_pytest_fixture_has_a_private_project_root(
    tmp_path, tmp_path_factory, pytestconfig
):
    with TemporaryDirectory(dir=tmp_path) as first, TemporaryDirectory(dir=tmp_path) as second:
        assert first != second
        assert Path(first).parent == Path(second).parent == tmp_path
        if os.name != "nt":
            assert stat.S_IMODE(Path(first).stat().st_mode) == 0o700
    base = tmp_path_factory.getbasetemp()
    assert base.name == "run" and base.parent.name.startswith("potbot-pytest-")
    assert tmp_path.is_relative_to(base)
    assert str(base.parent) in str(pytestconfig.getini("cache_dir"))
    pytestconfig.cache.set("temporary-storage/readback", {"ok": True})
    assert pytestconfig.cache.get("temporary-storage/readback", None) == {"ok": True}


def test_root_precedence_never_depends_on_global_temp_or_caller_cwd_after_creation(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    default = scratch_root()
    assert default == tmp_path / ".potbot" / "temporary"
    monkeypatch.setenv("POTBOT_TEMP_ROOT", str(tmp_path / "configured"))
    assert scratch_root() == tmp_path / "configured"
    assert scratch_root(tmp_path / "explicit") == tmp_path / "explicit"
    owner = TemporaryDirectory(dir=tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    owner.cleanup()
    assert not owner.path.exists()


def test_preflight_reports_access_failure_before_payload_work(tmp_path, monkeypatch):
    original = Path.replace

    def denied(path, target):
        if path.name == ".access-probe":
            raise PermissionError("fixture denied rename")
        return original(path, target)

    monkeypatch.setattr(Path, "replace", denied)
    with pytest.raises(TemporaryStorageError, match="preflight failed"):
        TemporaryDirectory(dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_sharing_violation_retries_boundedly_without_changing_acl(tmp_path, monkeypatch):
    owner = TemporaryDirectory(dir=tmp_path)
    original, calls = temporary._remove_tree, []

    def busy(path):
        calls.append(path)
        if len(calls) < 3:
            error = PermissionError("fixture sharing violation")
            error.winerror = 32
            raise error
        original(path)

    monkeypatch.setattr(temporary, "_WINDOWS", True)
    monkeypatch.setattr(temporary, "_remove_tree", busy)
    monkeypatch.setattr(temporary.time, "sleep", lambda _: None)
    owner.cleanup()
    assert len(calls) == 3 and not owner.path.exists()


def test_permanent_denial_is_explicit_and_keeps_the_owned_directory(tmp_path, monkeypatch):
    owner = TemporaryDirectory(dir=tmp_path)

    def denied(path):
        raise PermissionError("fixture permanent denial")

    with monkeypatch.context() as patch:
        patch.setattr(temporary, "_remove_tree", denied)
        with pytest.raises(TemporaryStorageError, match=str(owner.path).replace("\\", r"\\")):
            owner.cleanup()
        assert owner.path.is_dir()
    owner.cleanup()


def test_cleanup_failure_preserves_the_original_failure(tmp_path, monkeypatch):
    owner = TemporaryDirectory(dir=tmp_path)

    def denied(path):
        raise PermissionError("fixture permanent denial")

    with monkeypatch.context() as patch:
        patch.setattr(temporary, "_remove_tree", denied)
        with (
            pytest.warns(RuntimeWarning, match="retained"),
            pytest.raises(ValueError, match="original operation") as caught,
        ):
            with owner:
                raise ValueError("original operation")
        assert any("retained" in note for note in caught.value.__notes__)
    owner.cleanup()


@pytest.mark.parametrize("name", ["..", ".", "../outside", "/absolute", "a\\b", "a" * 65])
def test_temporary_names_cannot_escape_the_owned_root(tmp_path, name):
    with pytest.raises(ValueError):
        TemporaryDirectory(prefix=name, dir=tmp_path)


def test_repo_owned_scratch_allocations_cannot_regress_to_protected_tempfile_directories():
    root = Path(__file__).resolve().parents[2]
    files = [root / "conftest.py", *(root / "src").rglob("*.py"), *(root / "scripts").rglob("*.py")]
    violations = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "tempfile"
                and any(
                    n.name in {"TemporaryDirectory", "mkdtemp", "mkstemp", "NamedTemporaryFile"}
                    for n in node.names
                )
            ):
                violations.append(str(path.relative_to(root)))
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "tempfile"
                and node.attr in {"TemporaryDirectory", "mkdtemp", "mkstemp", "NamedTemporaryFile"}
            ):
                violations.append(str(path.relative_to(root)))
    assert violations == []


def test_all_standard_child_tempfile_apis_use_the_managed_environment(tmp_path):
    owner = TemporaryDirectory(dir=tmp_path)
    original = dict(os.environ)
    with owner:
        environment = owner.environment()
        assert all(
            environment[key] == owner.name for key in ("TMP", "TEMP", "TMPDIR", "POTBOT_TEMP_ROOT")
        )
        program = (
            "import os,tempfile; from pathlib import Path; "
            "fd,name=tempfile.mkstemp(suffix='.bin'); "
            "stream=os.fdopen(fd,'wb'); stream.write(bytes(range(256))); stream.close(); "
            "assert Path(name).read_bytes()==bytes(range(256)); "
            "print(Path(name).parent); Path(name).unlink()"
        )
        result = subprocess.run(
            [sys.executable, "-c", program],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert Path(result.stdout.strip()) == owner.path
        assert dict(os.environ) == original
    assert not owner.path.exists()
    with pytest.raises(TemporaryStorageError):
        owner.environment()


def test_atomic_file_is_closed_before_replacement_and_keeps_destination_acl(tmp_path):
    descriptor, path = temporary.allocate_file(tmp_path, prefix=".public-", suffix=".json")
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(b"complete bytes")
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    target = tmp_path / "café-日本語.json"
    path.replace(target)
    assert target.read_bytes() == b"complete bytes"


def test_atomic_file_retries_only_real_collisions_and_never_permission_denial(
    tmp_path, monkeypatch
):
    calls = []

    def denied(*args):
        calls.append(args)
        raise PermissionError("fixture inaccessible parent")

    monkeypatch.setattr(temporary.os, "open", denied)
    with pytest.raises(PermissionError):
        temporary.allocate_file(tmp_path)
    assert len(calls) == 1
    calls.clear()

    def collision(*args):
        calls.append(args)
        raise FileExistsError("fixture collision")

    monkeypatch.setattr(temporary.os, "open", collision)
    with pytest.raises(FileExistsError, match="collisions"):
        temporary.allocate_file(tmp_path)
    assert len(calls) == 8
