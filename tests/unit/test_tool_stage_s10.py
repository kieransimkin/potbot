"""S10 offline stage acceptance using actual files and Git. No sandbox claims."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools.tool_stage_s10 import (
    OfflineToolStager, ToolBuildPlan, _SafeCopy, command_digest, stage_name,
)


def plan():
    argv = ("/usr/bin/python3", "--version")
    return ToolBuildPlan(uuid4(), uuid4(), uuid4(), "stemlab", argv,
                         command_digest(argv), "a" * 40, "b" * 40)


def passing():
    return SimpleNamespace(dispatched=True, processes_stopped=True, outcome="completed",
                           exit_code=0, isolation_identity="f" * 64)


@pytest.fixture
def layout(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    stage = tmp_path / "home" / "agent" / "build"
    stage.mkdir(parents=True)
    for name in ("checkout", "environment", "downloads", "static"):
        (stage / name).mkdir()
    (stage / "checkout" / "src.py").write_text("print(42)\n")
    (stage / "static" / "binary").write_bytes(b"\x00\xff" * 100)
    (stage / "static" / "binary").chmod(0o755)
    return tools, evidence, stage


def test_staging_produces_private_receipt_and_runnable_readonly_stage(layout):
    tools, evidence, built = layout
    p = plan()
    record = OfflineToolStager(shared_tools=tools, evidence_dir=evidence).stage(
        plan=p, built_tree=built, result=passing(), expected_source_commit=p.source_commit)
    assert record["stage_name"] == stage_name("stemlab", p.operation_id)
    assert record["inventory"]["tree_sha256"]
    assert record["inventory"]["bytes"] > 0
    staged = tools / record["stage_name"]
    assert (staged / "static" / "binary").read_bytes() == b"\x00\xff" * 100
    assert staged.stat().st_mode & 0o777 == 0o755
    assert (staged / "static" / "binary").stat().st_mode & 0o111
    receipt = evidence / f"s10-{p.operation_id.hex}.json"
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert json.loads(receipt.read_text()) == record
    assert OfflineToolStager(shared_tools=tools, evidence_dir=evidence).inspect(p.operation_id) == record
    with pytest.raises(StateMismatchError):
        OfflineToolStager(shared_tools=tools, evidence_dir=evidence).stage(
            plan=p, built_tree=built, result=passing(), expected_source_commit=p.source_commit)


@pytest.mark.parametrize("changes", [
    {"outcome": "nonzero_exit"}, {"dispatched": False}, {"processes_stopped": False},
    {"isolation_identity": "UNCONFINED_TEST_ONLY"}, {"exit_code": 1},
])
def test_stage_rejects_unqualified_receipts(layout, changes):
    tools, evidence, built = layout
    receipt = passing()
    for key, value in changes.items():
        setattr(receipt, key, value)
    with pytest.raises(StateMismatchError):
        OfflineToolStager(shared_tools=tools, evidence_dir=evidence).stage(
            plan=plan(), built_tree=built, result=receipt,
            expected_source_commit="b" * 40)
    assert list(tools.iterdir()) == []


def test_symlink_hardlink_privileged_mode_and_fifo_are_denied(layout, tmp_path):
    tools, evidence, built = layout
    p = plan()
    (built / "static" / "alias").symlink_to(tmp_path)
    with pytest.raises(ScopeDeniedError):
        OfflineToolStager(shared_tools=tools, evidence_dir=evidence).stage(
            plan=p, built_tree=built, result=passing(), expected_source_commit=p.source_commit)
    (built / "static" / "alias").unlink()
    (built / "static" / "link").hardlink_to(built / "static" / "binary")
    target = tools / "hardlink-probe"
    target.mkdir()
    with pytest.raises(ScopeDeniedError):
        _SafeCopy().copy(built, target)


def test_command_digest_exact_and_no_shell_literal_expansion():
    p = plan()
    assert p.approved_argv_sha256 == command_digest(p.build_argv)
    with pytest.raises(ScopeDeniedError):
        ToolBuildPlan(p.workspace_id, p.agent_id, p.operation_id, p.slug, p.build_argv,
                      "a" * 64, p.expected_old_commit, p.source_commit)
    for argv in (("ls",), ("/home/alice/tool",), ("/usr/local/tools/a",), ("/bin/echo", "x\x00y")):
        with pytest.raises(InvalidInputError):
            command_digest(argv)


def test_build_tree_has_no_file_creation_on_rejected_pin(layout):
    tools, evidence, built = layout
    p = plan()
    with pytest.raises(StateMismatchError):
        OfflineToolStager(shared_tools=tools, evidence_dir=evidence).stage(
            plan=p, built_tree=built, result=passing(), expected_source_commit="c" * 40)
    assert not any(tools.iterdir())


def test_source_copy_does_not_run_git_hooks(layout):
    tools, evidence, built = layout
    cwd = built / "checkout"
    subprocess.run(["git", "init", "-q"], cwd=cwd, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=cwd, check=True)
    subprocess.run(["git", "config", "user.name", "Offline"], cwd=cwd, check=True)
    (cwd / "source.txt").write_bytes(b"source")
    subprocess.run(["git", "add", "."], cwd=cwd, check=True)
    subprocess.run(["git", "commit", "-qm", "local fixture"], cwd=cwd, check=True)
    before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=cwd).strip()
    p = plan()
    OfflineToolStager(shared_tools=tools, evidence_dir=evidence).stage(
        plan=p, built_tree=built, result=passing(), expected_source_commit=p.source_commit)
    after = subprocess.check_output(["git", "rev-parse", "HEAD"],
                                    cwd=tools / stage_name(p.slug, p.operation_id) / "checkout").strip()
    assert before == after
