"""Host-only Xpra log custody regression tests; never runs an Xpra daemon."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from potbot.tools.xpra_log_policy_s17 import XpraLogCustody, XpraLogError, inspect_xpra_logs


def fixture(tmp_path: Path):
    root, guest = tmp_path / "operator", tmp_path / "guest"
    root.mkdir(mode=0o700)
    guest.mkdir(mode=0o700)
    return root, guest


def test_operator_log_archived_with_digest_and_no_guest_file(tmp_path):
    root, guest = fixture(tmp_path)
    policy = XpraLogCustody(root, guest)
    out = policy.open()
    out.write(b"private-data-never-exposed-to-UI\n")
    assert policy.healthy
    policy.close(out)
    assert not policy.live_path.exists()
    doc = inspect_xpra_logs(root)
    assert doc["requiresReconciliation"] is False
    assert doc["instances"][0]["archived"] == 1
    assert "private-data" not in json.dumps(doc)
    assert list(guest.iterdir()) == []


def test_unexpected_stale_live_log_fences_start(tmp_path):
    root, guest = fixture(tmp_path)
    owner = XpraLogCustody(root, guest)
    owner.open().write(b"crash-before-finalize")
    assert inspect_xpra_logs(root)["requiresReconciliation"]
    with pytest.raises(XpraLogError):
        XpraLogCustody(root, guest).open()


def test_incomplete_archive_receipt_requires_reconciliation(tmp_path):
    root, guest = fixture(tmp_path)
    owner = XpraLogCustody(root, guest)
    out = owner.open()
    out.write(b"evidence")
    owner.close(out)
    receipt = next(owner.instance_dir.glob("*.json"))
    receipt.unlink()
    doc = inspect_xpra_logs(root)
    assert doc["requiresReconciliation"] is True
    assert doc["instances"][0]["orphanedArchives"] == 1


def test_archive_tamper_and_suspicious_files_fail_closed(tmp_path):
    root, guest = fixture(tmp_path)
    owner = XpraLogCustody(root, guest)
    out = owner.open()
    out.write(b"ok")
    owner.close(out)
    archive = next(owner.instance_dir.glob("*.log"))
    archive.write_bytes(b"not-the-same")
    with pytest.raises(XpraLogError):
        inspect_xpra_logs(root)


def test_symlink_and_accessible_log_root_rejected(tmp_path):
    root, guest = fixture(tmp_path)
    root.chmod(0o755)
    with pytest.raises(XpraLogError):
        XpraLogCustody(root, guest)
    root.chmod(0o700)
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(XpraLogError):
        XpraLogCustody(link, guest)
    with pytest.raises(XpraLogError):
        XpraLogCustody(guest, guest)


def test_log_limit_fault_does_not_destroy_evidence(tmp_path, monkeypatch):
    from potbot.tools import xpra_log_policy_s17 as module
    root, guest = fixture(tmp_path)
    monkeypatch.setattr(module, "_MAX_ACTIVE", 4)
    owner = XpraLogCustody(root, guest)
    output = owner.open()
    output.write(b"exceeded")
    output.flush()
    assert not owner.healthy
    with pytest.raises(XpraLogError):
        owner.close(output)
    assert owner.live_path.exists()
    with pytest.raises(XpraLogError):
        owner.open()


def test_qualifier_does_not_follow_symlinked_archive(tmp_path):
    root, guest = fixture(tmp_path)
    owner = XpraLogCustody(root, guest)
    out = owner.open()
    out.write(b"ok")
    owner.close(out)
    archive = next(owner.instance_dir.glob("*.log"))
    archive.unlink()
    archive.symlink_to("/etc/passwd")
    with pytest.raises(XpraLogError):
        inspect_xpra_logs(root)


def test_orphaned_archive_fences_new_start(tmp_path):
    root, guest = fixture(tmp_path)
    owner = XpraLogCustody(root, guest)
    handle = owner.open()
    handle.write(b"log")
    owner.close(handle)
    next(owner.instance_dir.glob("*.json")).unlink()
    with pytest.raises(XpraLogError):
        XpraLogCustody(root, guest).open()


def test_qualifier_creates_private_no_content_report(tmp_path):
    from scripts.qualify_xpra_logs_s17 import main
    root, guest = fixture(tmp_path)
    owner = XpraLogCustody(root, guest)
    handle = owner.open()
    handle.write(b"secret-no-disclosure")
    owner.close(handle)
    report = tmp_path / "report.json"
    assert main(["--log-root", str(root), "--report", str(report)]) == 0
    assert "secret-no-disclosure" not in report.read_text()
    assert report.stat().st_mode & 0o077 == 0
