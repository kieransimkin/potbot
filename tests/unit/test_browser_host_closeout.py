"""Manifest validation only; passing data here is not live-host evidence."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "host_closeout", ROOT / "scripts/check_browser_host_closeout.py"
)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


def make(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sources = (
        "src/potbot/tools/xpra_browser_worker.py",
        "src/potbot/tools/xpra_browser_control.py",
        "src/potbot/tools/agent_xpra_streams.py",
    )
    pins = {}
    for path in sources:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# synthetic validator fixture\n")
        pins[path] = hashlib.sha256(target.read_bytes()).hexdigest()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=test",
                    "-c", "user.email=test@example.invalid", "commit", "-qm", "initial"], check=True)
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    directory = tmp_path / "operator-reports"
    directory.mkdir(mode=0o700)
    workspace, agents, image = str(uuid4()), [str(uuid4()), str(uuid4())], "a" * 64
    drills = {}
    for k in GATE.REQUIRED:
        path = directory / (k + ".json")
        path.write_text(json.dumps({"synthetic": k}) + "\n")
        path.chmod(0o600)
        drills[k] = {
            "passed": True, "workspace_id": workspace, "agent_ids": agents,
            "image_digest": image, "operator_epoch": 2, "evidence_file": path.name,
            "evidence_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "observed_process_tree_stopped": True, "operator_observation": "direct_live_host",
        }
    manifest = {
        "schema": GATE.SCHEMA, "source": {"commit": sha, "tree_dirty": False, "files": pins},
        "workspace_id": workspace, "agent_ids": agents, "image_digest": image,
        "qualification_sha256": "b" * 64, "operator_epoch": 2, "drills": drills,
    }
    return repo, directory, manifest


def test_complete_synthetic_manifest_is_validated_but_not_attested(tmp_path):
    repo, directory, manifest = make(tmp_path)
    outcome = GATE.verify(manifest, directory=directory, root=repo)
    assert outcome["manifest_valid"] is True
    assert outcome["machine_checked_drills"] == len(GATE.REQUIRED)
    assert outcome["production_qualified"] is False
    assert outcome["operator_signoff_required"] is True


@pytest.mark.parametrize("change", [
    lambda x: x["drills"].pop("private_x11_and_home"),
    lambda x: x["agent_ids"].__setitem__(1, x["agent_ids"][0]),
    lambda x: x.update(operator_epoch=True),
    lambda x: x["source"].update(tree_dirty=True),
    lambda x: x["drills"]["view_only_streams"].update(passed=False),
    lambda x: x["drills"]["ticket_replay_and_epoch_denial"].update(operator_observation="model_report"),
    lambda x: x["drills"]["operator_loss_fencing"].update(observed_process_tree_stopped=False),
    lambda x: x["drills"]["postgres_artifacts_and_rls"].update(evidence_sha256="0" * 64),
])
def test_false_or_incomplete_host_claims_are_rejected(tmp_path, change):
    repo, directory, data = make(tmp_path)
    change(data)
    with pytest.raises(ValueError):
        GATE.verify(data, directory=directory, root=repo)


def test_source_changes_invalidates_committed_host_report(tmp_path):
    repo, directory, data = make(tmp_path)
    (repo / "src/potbot/tools/xpra_browser_control.py").write_text("# changed\n")
    with pytest.raises(ValueError):
        GATE.verify(data, directory=directory, root=repo)


def test_private_symlink_and_non_private_files_are_rejected(tmp_path):
    repo, directory, data = make(tmp_path)
    link = directory / "link.json"
    link.symlink_to(directory / "agent_logs_and_archive.json")
    data["drills"]["agent_logs_and_archive"]["evidence_file"] = link.name
    with pytest.raises(ValueError):
        GATE.verify(data, directory=directory, root=repo)
    link.unlink()
    original = directory / "agent_logs_and_archive.json"
    original.chmod(0o644)
    data["drills"]["agent_logs_and_archive"]["evidence_file"] = original.name
    with pytest.raises(ValueError):
        GATE.verify(data, directory=directory, root=repo)


@pytest.mark.parametrize("raw", [b"", b"{}{}", b'{"x":1,"x":2}', b'{"x":NaN}', b"x" * (96 * 1024 + 1)])
def test_rejects_invalid_host_json(raw):
    with pytest.raises(ValueError):
        GATE.unique_json(raw)
