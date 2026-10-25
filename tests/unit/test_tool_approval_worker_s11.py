"""S11 host worker always fences uncertain activation and requires runtime repin."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.domain.errors import StateMismatchError
from potbot.tools.tool_approval_worker_s11 import ToolApprovalWorker


@pytest.fixture
def rig(tmp_path):
    workspace, agent, operation = uuid4(), uuid4(), uuid4()
    parent = tmp_path / "operator"
    parent.mkdir(mode=0o700)
    home = tmp_path / "home"; home.mkdir()
    tools = tmp_path / "tools"; tools.mkdir()
    context = SimpleNamespace(workspace_id=workspace)
    claim = {"id": operation, "worker_token": uuid4(), "agent_id": agent,
             "slug": "stemlab", "old_commit": "a" * 40, "new_commit": "b" * 40,
             "evidence_sha256": "c" * 64, "suite_sha256": "d" * 64,
             "passed_tests": 2, "tests": [
                 {"name": "smoke", "kind": "smoke", "elapsedMs": 10, "passed": True},
                 {"name": "unit", "kind": "unit", "elapsedMs": 13, "passed": True}]}
    approvals = SimpleNamespace(
        claim=lambda ctx, **kwargs: claim,
        finished=[],
        finish=lambda ctx, **kwargs: approvals.finished.append(kwargs),
    )
    tested = {"id": str(operation), "slug": "stemlab", "old_commit": "a"*40,
              "new_commit": "b"*40, "test_evidence_sha256": "c"*64, "state": "ready",
              "suite_sha256": "d"*64, "test_results": [
                  {"name": "smoke", "kind": "smoke", "elapsed_ms": 10, "passed": True},
                  {"name": "unit", "kind": "unit", "elapsed_ms": 13, "passed": True}]}
    manager = SimpleNamespace(
        runtime=SimpleNamespace(home=home, tool_entrypoints=()),
        store=SimpleNamespace(root=tools),
        inspect=lambda **kwargs: tested,
        activate=lambda **kwargs: {"state": "active", "new_commit": "b"*40},
        runtime_entrypoint_updates=lambda **kwargs: {
            "requires_operator_runtime_repin": True,
            "tested_sha256": "c"*64, "pins": {}, "remove_prior_pins": []},
    )
    worker = ToolApprovalWorker(approvals=approvals, manager=manager,
                                context=context, proposal_dir=parent, agent_id=agent, setup_token="S"*64)
    return worker, claim, approvals, manager, parent, tested


def test_success_still_requires_runtime_repin(rig):
    worker, claim, approvals, _, private, _ = rig
    result = worker.run_once()
    assert result["state"] == "awaiting_repin"
    assert approvals.finished == [{"operation": claim["id"], "worker_token": claim["worker_token"], "active": True}]
    assert (private / f"s11-repin-{claim['id'].hex}.json").is_file()
    assert (private / f"s11-repin-{claim['id'].hex}.json").stat().st_mode & 0o777 == 0o600


def test_evidence_mismatch_prevents_activation_and_marks_indeterminate(rig):
    worker, claim, approvals, manager, _, tested = rig
    tested["test_evidence_sha256"] = "d" * 64
    invocations = []
    manager.activate = lambda **kwargs: invocations.append(kwargs)
    with pytest.raises(StateMismatchError):
        worker.run_once()
    assert not invocations
    assert approvals.finished[0]["active"] is False


def test_failure_to_persist_repin_is_indeterminate(rig):
    worker, claim, approvals, _, private, _ = rig
    (private / f"s11-repin-{claim['id'].hex}.json").write_text("already exists")
    with pytest.raises(FileExistsError):
        worker.run_once()
    assert approvals.finished[0]["active"] is False


def test_no_approved_lease_does_not_activate(rig):
    worker, claim, approvals, manager, _, _ = rig
    approvals.claim = lambda ctx, **kwargs: None
    manager.activate = lambda **kwargs: pytest.fail("unexpected activation")
    assert worker.run_once() == {"state": "idle"}


def test_pending_repin_refuses_unapproved_executable_hashes(rig):
    worker, claim, approvals, manager, _, tested = rig
    approvals.inspect = lambda ctx, op: {"state": "awaiting_repin", "evidenceSha256": "c"*64}
    approvals.confirm_repin = lambda *args, **kwargs: pytest.fail("premature completion")
    tested["state"] = "active"
    manager.runtime_entrypoint_updates = lambda **kwargs: {"pins": {"/usr/local/tools/stemlab/static/app": "a"*64}, "remove_prior_pins": []}
    with pytest.raises(StateMismatchError):
        worker.confirm_repin(operation=claim["id"], agent_id=claim["agent_id"])


def test_forged_test_summary_blocks_activation(rig):
    worker, claim, approvals, manager, _, _ = rig
    claim["tests"][0]["elapsedMs"] += 1
    invocations = []
    manager.activate = lambda **kwargs: invocations.append(kwargs)
    with pytest.raises(StateMismatchError):
        worker.run_once()
    assert invocations == []
    assert approvals.finished[0]["active"] is False
