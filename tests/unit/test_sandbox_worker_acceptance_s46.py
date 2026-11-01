"""S46 validator tests; synthetic records are NOT live qualification evidence."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.evaluation import sandbox_worker_acceptance as acceptance

ROOT = Path(__file__).resolve().parents[2]


def config():
    return {
        "schema_version": 1,
        "workspace_id": str(uuid4()),
        "principal_id": str(uuid4()),
        "agent_ids": [str(uuid4()), str(uuid4())],
        "workspace_name": "probe-workspace",
        "ledger": "/srv/operator/workspace.db",
        "runtime": {
            "image_digest": "a" * 64,
            "qualification_sha256": "b" * 64,
            "rootfs": "/srv/images/fixture",
            "home": "/srv/workspaces/fixture/home",
            "model_staging": "/srv/staging",
            "shared_models": "/srv/models",
            "shared_tools": "/srv/tools",
        },
    }


def audit(workspace, run):
    return {
        "schema": "potbot-gui-audit-integrity-s16/v1",
        "qualified": True,
        "workspaceId": workspace,
        "runId": run,
        "identityKind": "agent",
        "tracedProcesses": 2,
        "fileKeys": 4,
        "fileEvents": 10,
        "cpuUsec": 100,
        "readBytes": 200,
        "writeBytes": 0,
        "peakMemoryBytes": 4096,
    }


def test_policy_is_exact_and_independent():
    source = config()
    checked = acceptance.policy(source)
    source["agent_ids"].clear()
    assert len(checked["agent_ids"]) == 2


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c.update(schema_version=True),
        lambda c: c.update(argv=["/bin/sh"]),
        lambda c: c.update(agent_ids=[]),
        lambda c: c.update(agent_ids=[c["agent_ids"][0]] * 2),
        lambda c: c.update(agent_ids=[str(uuid4()) for _ in range(5)]),
        lambda c: c.update(workspace_id="not-a-uuid"),
        lambda c: c.update(workspace_name="../operator"),
        lambda c: c.update(ledger="/srv/workspaces/fixture/home/ledger"),
        lambda c: c["runtime"].update(rootfs="/srv/images/../fixture"),
        lambda c: c["runtime"].update(rootfs="/srv//fixture"),
        lambda c: c["runtime"].update(home=c["runtime"]["rootfs"]),
        lambda c: c["runtime"].update(shared_tools=c["runtime"]["home"] + "/tools"),
        lambda c: c["runtime"].update(image_digest="unknown"),
        lambda c: c["runtime"].update(tool_entrypoints=[["/bin/sh", "a" * 64]]),
    ],
)
def test_policy_rejects_scope_expansion(mutation):
    value = config()
    mutation(value)
    with pytest.raises((ValueError, TypeError)):
        acceptance.policy(value)


@pytest.mark.parametrize(
    "raw",
    [b"{}{}", b"[]", b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b"\xff", b" " * 65537],
)
def test_json_rejects_ambiguous_or_unbounded_documents(raw):
    with pytest.raises(ValueError):
        acceptance.unique_json(raw)


def valid_probe():
    run, nonce = str(uuid4()), "c" * 64
    value = {
        "nonce": nonce,
        "invocation_id": run,
        "mode": "inspect",
        "observation": {
            "uid": 420,
            "gid": 69,
            "pid": 3,
            "cwd": "/home/agent",
            "root_readonly": True,
            "inet_denied": True,
            "inet6_denied": True,
        },
    }
    expected = dict(nonce=nonce, run_id=run, uid=420, gid=69, guest_cwd="/home/agent")
    return value, expected


def test_probe_binds_nonce_run_identity_and_boundary_observations():
    value, expected = valid_probe()
    assert acceptance.check_probe(value, **expected)["uid"] == 420


@pytest.mark.parametrize(
    "mutation",
    [
        lambda v: v.update(nonce="d" * 64),
        lambda v: v.update(invocation_id=str(uuid4())),
        lambda v: v.update(mode="wire_only"),
        lambda v: v.update(observation=None),
        lambda v: v["observation"].update(uid=0),
        lambda v: v["observation"].update(pid=True),
        lambda v: v["observation"].update(gid=True),
        lambda v: v["observation"].update(cwd="/home/another-agent"),
        lambda v: v["observation"].update(root_readonly=False),
        lambda v: v["observation"].update(inet_denied=1),
        lambda v: v["observation"].update(inet6_denied=False),
        lambda v: v["observation"].update(extra="unadmitted"),
    ],
)
def test_probe_rejects_missing_replayed_or_unconfined_responses(mutation):
    value, expected = valid_probe()
    mutation(value)
    with pytest.raises(ValueError):
        acceptance.check_probe(value, **expected)


def process():
    return SimpleNamespace(
        dispatched=True,
        processes_stopped=True,
        outcome="completed",
        exit_code=0,
        stdout_truncated=False,
        stderr_truncated=False,
        isolation_identity="unit-synthetic-isolation",
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("dispatched", False),
        ("processes_stopped", False),
        ("processes_stopped", 1),
        ("outcome", "inconclusive"),
        ("exit_code", False),
        ("exit_code", 1),
        ("stdout_truncated", True),
        ("stderr_truncated", True),
        ("isolation_identity", ""),
    ],
)
def test_process_requires_confirmed_bounded_success(field, value):
    result = process()
    setattr(result, field, value)
    with pytest.raises(ValueError):
        acceptance.check_process(result)


def test_process_valid_record():
    acceptance.check_process(process())


@pytest.mark.parametrize(
    "field,value",
    [
        ("qualified", 1),
        ("workspaceId", str(uuid4())),
        ("runId", str(uuid4())),
        ("identityKind", "principal"),
        ("tracedProcesses", 0),
        ("fileEvents", 0),
        ("fileKeys", -1),
        ("cpuUsec", True),
        ("readBytes", None),
    ],
)
def test_audit_rejects_wrong_scope_and_unmeasured_counters(field, value):
    ws, run = str(uuid4()), str(uuid4())
    record = audit(ws, run)
    record[field] = value
    with pytest.raises(ValueError):
        acceptance.check_audit(record, workspace_id=ws, run_id=run)


def report_arguments():
    ws, agents = str(uuid4()), [str(uuid4()), str(uuid4())]
    attempts = []
    for agent in agents:
        run = str(uuid4())
        attempts.append(
            {
                "agent_id": agent,
                "run_id": run,
                "qualified": True,
                "process_stopped": True,
                "audit": audit(ws, run),
                "isolation_sha256": "e" * 64,
                "registered_identity": {"uid": 420, "gid": 69},
                "observations": {
                    "uid": 420,
                    "gid": 69,
                    "root_readonly": True,
                    "inet_denied": True,
                    "inet6_denied": True,
                },
            }
        )
    return dict(
        source_commit="a" * 40,
        source_hashes={acceptance.PROBE_SOURCE: "b" * 64},
        image_digest="c" * 64,
        qualification_sha256="d" * 64,
        probe_sha256="b" * 64,
        workspace_id=ws,
        agents=agents,
        attempts=attempts,
    )


def test_report_does_not_claim_browser_or_provenance_acceptance():
    value = acceptance.report(**report_arguments())
    assert value["qualified_smoke"] is True
    assert "nested_cog_workflows" in value["not_qualified"]
    assert "xpra_browser_streams" in value["not_qualified"]
    assert value["qualification"] == "registered_worker_transport_and_kernel_audit_only"


@pytest.mark.parametrize(
    "case", ["missing", "duplicate", "unconfirmed", "wrong-agent", "no-source"]
)
def test_report_requires_every_selected_attempt(case):
    args = report_arguments()
    if case == "missing":
        args["attempts"].pop()
    elif case == "duplicate":
        args["attempts"][1]["run_id"] = args["attempts"][0]["run_id"]
    elif case == "unconfirmed":
        args["attempts"][0]["process_stopped"] = False
    elif case == "wrong-agent":
        args["attempts"][0]["agent_id"] = str(uuid4())
    else:
        args["source_commit"] = "uncommitted"
    with pytest.raises(ValueError):
        acceptance.report(**args)


@pytest.mark.skipif(os.name != "posix", reason="operator files are POSIX host controls")
def test_private_file_and_exclusive_report_do_not_relax_permissions(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "report.json"
    value = acceptance.report(**report_arguments())
    acceptance.write_report(path, value)
    raw = acceptance.read_private(path)
    assert json.loads(raw)["qualified_smoke"] is True
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        acceptance.write_report(path, {"qualified_smoke": False})
    assert path.read_bytes() == raw
    linked = tmp_path / "redirect.json"
    linked.symlink_to(path)
    with pytest.raises(ValueError):
        acceptance.read_private(linked)
    alias = tmp_path / "hardlink.json"
    os.link(path, alias)
    with pytest.raises(ValueError):
        acceptance.read_private(path)
    alias.unlink()
    path.chmod(0o644)
    with pytest.raises(ValueError):
        acceptance.read_private(path)


def test_live_runner_is_explicit_and_never_replaces_a_report(tmp_path, capsys):
    location = ROOT / "scripts/qualify_sandbox_cog_s46.py"
    spec = importlib.util.spec_from_file_location("qualification_s46", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path / "result.json"
    target.write_text("existing evidence")
    args = ["--policy", str(tmp_path / "missing.json"), "--report", str(target)]
    assert module.main(args) == 1
    assert module.main([*args, "--run-live"]) == 1
    assert target.read_text() == "existing evidence"
    assert '"qualified_smoke": false' in capsys.readouterr().err


@pytest.mark.parametrize("case", ["probe-digest", "isolation", "observations", "uid", "network"])
def test_report_rejects_unbound_probe_or_missing_boundary_evidence(case):
    args = report_arguments()
    attempt = args["attempts"][0]
    if case == "probe-digest":
        args["probe_sha256"] = "f" * 64
    elif case == "isolation":
        attempt.pop("isolation_sha256")
    elif case == "observations":
        attempt.pop("observations")
    elif case == "uid":
        attempt["observations"]["uid"] = 0
    else:
        attempt["observations"]["inet_denied"] = False
    with pytest.raises(ValueError):
        acceptance.report(**args)


@pytest.mark.skipif(os.name != "posix", reason="operator files are POSIX host controls")
def test_failed_report_flush_does_not_leave_a_success_document(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    target = tmp_path / "new-report.json"

    def broken(_):
        raise OSError("injected fsync failure")

    with monkeypatch.context() as patch:
        patch.setattr(acceptance.os, "fsync", broken)
        with pytest.raises(OSError, match="fsync"):
            acceptance.write_report(target, acceptance.report(**report_arguments()))
    assert not target.exists()


def _runner_module():
    location = ROOT / "scripts/qualify_sandbox_cog_s46.py"
    spec = importlib.util.spec_from_file_location("qualification_s46_source_test", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_source_check_rejects_dirty_diagnostic_bytes(tmp_path, monkeypatch):
    import subprocess

    repo = tmp_path / "source-repo"
    repo.mkdir()
    source = repo / "probe.py"
    source.write_bytes(b"# diagnostic fixture\n")

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, stderr=subprocess.DEVNULL)

    git("init", "-q")
    git("add", "probe.py")
    git(
        "-c",
        "user.name=S46 fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    module = _runner_module()
    monkeypatch.setattr(module, "SOURCES", ("probe.py",))
    head, hashes = module.committed_sources(repo)
    assert head == git("rev-parse", "HEAD").decode().strip()
    assert hashes == {"probe.py": acceptance.digest(source.read_bytes())}
    source.write_bytes(b"# changed after commit\n")
    with pytest.raises(ValueError, match="committed source"):
        module.committed_sources(repo)


def test_live_runner_cannot_bypass_executor_or_provision_authority():
    import ast

    tree = ast.parse((ROOT / "scripts/qualify_sandbox_cog_s46.py").read_text())
    calls = [node.func for node in ast.walk(tree) if isinstance(node, ast.Call)]
    names = {node.id for node in calls if isinstance(node, ast.Name)}
    methods = {node.attr for node in calls if isinstance(node, ast.Attribute)}
    assert {
        "SandboxedCogExecutor",
        "SharedWorkspaceBubblewrapBackend",
        "verify_gui_run",
        "validate_runtime_pin",
    } <= names
    assert not {"__new__", "pin_image", "provision", "reconcile"} & methods
