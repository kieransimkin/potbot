"""Filesystem publication and confirmed-stop boundaries before durable receipts."""

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import IndeterminateExecutionError, ScopeDeniedError
from potbot.domain.execution import ActionProposal, CapabilityContext, ToolDefinition, ToolRegistry
from potbot.tools.gateway import ToolGateway
from potbot.tools.repository import Lease
from potbot.tools.secrets import CaptureRedactionError, CaptureRedactor
from potbot.tools.supervisor import ProcessResult


def gateway_fixture(tmp_path):
    root = tmp_path / "candidate"
    receipts_root = tmp_path / "private" / "receipts"
    artifacts_root = tmp_path / "private" / "artifacts"
    for path in (root, receipts_root, artifacts_root):
        path.mkdir(parents=True)
    definition = ToolDefinition("fixture", "test", (sys.executable, "-I", "-c", "pass"))
    receipts = SimpleNamespace(root=receipts_root, write=Mock())
    gateway = ToolGateway(
        Mock(),
        SimpleNamespace(backend=SimpleNamespace(root=artifacts_root)),
        Mock(),
        ToolRegistry((definition,)),
        CapabilityContext(("fixture",)),
        receipts,
        supervisor=Mock(),
    )
    return SimpleNamespace(gateway=gateway, root=root, definition=definition, receipts=receipts)


@pytest.mark.parametrize("private_kind", ["receipts", "artifacts"])
@pytest.mark.parametrize("overlap", ["same", "ancestor", "descendant", "file"])
def test_private_storage_cannot_be_exposed_by_an_explicit_runtime_mount(
    tmp_path, private_kind, overlap
):
    rig = gateway_fixture(tmp_path)
    private = getattr(rig.gateway, private_kind)
    private_root = private.root if private_kind == "receipts" else private.backend.root
    if overlap == "same":
        mount = private_root
    elif overlap == "ancestor":
        mount = private_root.parent
    elif overlap == "descendant":
        mount = private_root / "workspace"
        mount.mkdir()
    else:
        mount = private_root / "private-record.json"
        mount.write_text("{}")
    rig.gateway.read_only_roots = (mount,)
    with pytest.raises(ScopeDeniedError, match="private controller storage"):
        rig.gateway._paths(rig.root, ActionProposal("fixture"))


def test_default_runtime_mounts_also_cannot_expose_private_storage(tmp_path, monkeypatch):
    rig = gateway_fixture(tmp_path)
    monkeypatch.setattr("potbot.tools.gateway.runtime_roots", lambda: (rig.receipts.root,))
    with pytest.raises(ScopeDeniedError, match="private controller storage"):
        rig.gateway._paths(rig.root, ActionProposal("fixture"))


def test_separate_runtime_assets_and_safe_relative_paths_remain_usable(tmp_path):
    rig = gateway_fixture(tmp_path)
    assets = tmp_path / "runtime-assets"
    assets.mkdir()
    rig.gateway.read_only_roots = (assets,)
    assert rig.gateway._paths(rig.root, ActionProposal("fixture")) == rig.root
    (rig.root / "escape").symlink_to(assets, target_is_directory=True)
    with pytest.raises(ScopeDeniedError, match="escapes"):
        rig.gateway._paths(rig.root, ActionProposal("fixture", output_paths=("escape/result",)))


@pytest.mark.parametrize("placement", ["contents", "filename", "directory", "symlink_target"])
def test_secret_material_in_file_contents_or_snapshot_metadata_blocks_publication(
    tmp_path, placement
):
    canary = "credential-canary-Q2zX7"
    if placement == "contents":
        (tmp_path / "result.txt").write_text(canary)
    elif placement == "filename":
        (tmp_path / f"result-{canary}.txt").write_text("ordinary output")
    elif placement == "directory":
        (tmp_path / canary).mkdir()
    else:
        (tmp_path / "result-link").symlink_to(f"missing-{canary}")
    with pytest.raises(CaptureRedactionError, match="secret material"):
        ToolGateway._check_secret_files(tmp_path, CaptureRedactor((canary,)))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO boundary requires POSIX")
def test_fifo_is_rejected_without_waiting_for_a_writer(tmp_path):
    generated = tmp_path / "generated"
    generated.mkdir()
    os.mkfifo(generated / "never-opened-by-a-writer")
    # A separate bounded process makes a regression fail instead of hanging pytest.
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from potbot.tools.gateway import ToolGateway\n"
        "from potbot.tools.secrets import CaptureRedactor, CaptureRedactionError\n"
        "try:\n"
        "    ToolGateway._check_secret_files(Path(sys.argv[1]), CaptureRedactor(('canary',)))\n"
        "except CaptureRedactionError:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(2)\n"
    )
    result = subprocess.run(
        (sys.executable, "-c", script, str(tmp_path)), capture_output=True, timeout=5, check=False
    )
    assert result.returncode == 0, result.stderr.decode()


def test_secret_scan_accepts_ordinary_binary_and_text_files(tmp_path):
    (tmp_path / "binary.bin").write_bytes(b"\x00\xffordinary bytes")
    (tmp_path / "text.txt").write_text("ordinary output")
    ToolGateway._check_secret_files(tmp_path, CaptureRedactor(("credential-canary",)))


@pytest.mark.parametrize("reported_outcome", ["completed", "inconclusive"])
def test_unconfirmed_process_stop_cannot_publish_a_receipt_or_completion(
    tmp_path, reported_outcome
):
    rig = gateway_fixture(tmp_path)
    gateway = rig.gateway
    context = WorkspaceContext(uuid4(), uuid4())
    action_id, reservation_id, run_id, candidate_id = (uuid4() for _ in range(4))
    lease = Lease(candidate_id, "fixture-owner", 1, datetime.now(UTC) + timedelta(minutes=1))
    run = {"id": run_id, "episode_id": uuid4(), "input_fingerprint": "a" * 64}
    gateway.store = SimpleNamespace(
        inspect=Mock(return_value={"run": None, "action": {"kind": "fixture"}})
    )
    gateway._admit = Mock(
        return_value=(run, rig.definition, ActionProposal("fixture"), {"path": str(rig.root)})
    )
    gateway._uncertain = Mock()
    gateway._complete = Mock()
    gateway._fingerprint = Mock()
    gateway.supervisor.run.return_value = ProcessResult(
        reported_outcome,
        0 if reported_outcome == "completed" else None,
        b"unconfirmed output",
        b"",
        10,
        False,
        False,
        True,
        "fixture-isolation",
        processes_stopped=False,
    )
    with pytest.raises(IndeterminateExecutionError, match="termination is unconfirmed"):
        gateway.execute(context, action_id, lease, reservation_id)
    rig.receipts.write.assert_not_called()
    gateway._complete.assert_not_called()
    gateway._fingerprint.assert_not_called()
    gateway._uncertain.assert_called_once_with(context, run, lease)
