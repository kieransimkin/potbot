"""Linux inotify integration and complete journal-chain verification (S7)."""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import pytest

from potbot.domain.errors import CapabilityUnavailableError, ScopeDeniedError, StateMismatchError
from potbot.tools.ai_config_history import AiConfigHistoryMonitor, _scan
from potbot.tools.ai_history_audit import audit_ai_history

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="inotify and dirfd require Linux")


def roots(tmp_path: Path) -> tuple[Path, Path]:
    config, past = tmp_path / "config", tmp_path / "history"
    config.mkdir()
    past.mkdir()
    return config, past


def sample_history(config: Path, past: Path) -> None:
    (config / "a.txt").write_text("first\n", encoding="utf-8")
    with AiConfigHistoryMonitor(config_dir=config, past_dir=past, interval_seconds=5) as monitor:
        (config / "a.txt").write_text("second\n", encoding="utf-8")
        assert monitor.poll_once() is not None
        (config / "b.bin").write_bytes(b"\0\xff\0")
        assert monitor.poll_once() is not None
        (config / "a.txt").unlink()
        assert monitor.poll_once() is not None


def test_full_history_replay_and_live_comparison(tmp_path: Path) -> None:
    config, past = roots(tmp_path)
    sample_history(config, past)
    report = audit_ai_history(past, config_dir=config)
    assert report.latest_sequence == 3
    assert report.verified_revisions == 3
    assert report.matches_live_tree is True
    assert report.current_digest != report.original_digest
    assert report.checked_archive_bytes > 0
    assert report.to_dict()["verified"] is True
    assert set(report.to_dict()).isdisjoint({"snapshot", "data_base64"})
    # Restart must replay older reversals, not merely trust the current head.
    with AiConfigHistoryMonitor(config_dir=config, past_dir=past) as monitor:
        monitor.require_active()


def test_tampered_head_bytes_rejected_even_when_public_digest_unchanged(tmp_path: Path) -> None:
    config, past = roots(tmp_path)
    sample_history(config, past)
    head = past / "head.json"
    body = json.loads(head.read_text())
    assert body["snapshot"]["b.bin"]["type"] == "file"
    # S6's identity deliberately excludes data_base64. Audit checks content
    # bytes against the recorded digest, closing that tampering hole.
    body["snapshot"]["b.bin"]["data_base64"] = base64.b64encode(b"FAKE").decode()
    head.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(StateMismatchError):
        audit_ai_history(past)
    with pytest.raises(StateMismatchError):
        AiConfigHistoryMonitor(config_dir=config, past_dir=past).start()


def test_tampered_prior_revision_or_diff_blocks_restart(tmp_path: Path) -> None:
    config, past = roots(tmp_path)
    sample_history(config, past)
    diff = past / "000000000001.reverse.diff"
    diff.write_text("malicious fake diff\n", encoding="utf-8")
    with pytest.raises(StateMismatchError):
        audit_ai_history(past)
    with pytest.raises(StateMismatchError):
        AiConfigHistoryMonitor(config_dir=config, past_dir=past).start()


def test_incomplete_or_orphaned_history_fails_closed(tmp_path: Path) -> None:
    config, past = roots(tmp_path)
    sample_history(config, past)
    (past / "000000000002.reverse.json").unlink()
    with pytest.raises(StateMismatchError):
        audit_ai_history(past)
    (past / "000000000002.reverse.json").write_text("{}", encoding="utf-8")
    with pytest.raises(StateMismatchError):
        audit_ai_history(past)
    (past / "malicious-run.tmp").write_bytes(b"junk")
    with pytest.raises(StateMismatchError):
        audit_ai_history(past)


def test_audit_requires_operator_lock_and_refuses_running_writer(tmp_path: Path) -> None:
    config, past = roots(tmp_path)
    with AiConfigHistoryMonitor(config_dir=config, past_dir=past) as monitor:
        with pytest.raises(StateMismatchError):
            audit_ai_history(past)
        monitor.require_active()
    assert audit_ai_history(past).latest_sequence == 0


def test_audit_limits_fail_closed(tmp_path: Path) -> None:
    config, past = roots(tmp_path)
    sample_history(config, past)
    with pytest.raises(CapabilityUnavailableError):
        audit_ai_history(past, max_revisions=1)
    with pytest.raises(CapabilityUnavailableError):
        audit_ai_history(past, max_archive_bytes=1)


def test_dirfd_scanning_does_not_follow_raced_parent_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _ = roots(tmp_path)
    inner = config / "inner"
    inner.mkdir()
    (inner / "normal.txt").write_text("public")
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret.txt").write_text("not yours")
    original_open = os.open
    changed = False

    def switched(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal changed
        if path == "inner" and "dir_fd" in kwargs and not changed:
            changed = True
            inner.rename(config / "detached")
            inner.symlink_to(external, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", switched)
    with pytest.raises((StateMismatchError, ScopeDeniedError)):
        _scan(config, max_files=50, max_bytes=100000, max_file_bytes=50000)
    assert changed
    assert (external / "secret.txt").read_text() == "not yours"


def test_hardlinked_config_file_rejected(tmp_path: Path) -> None:
    config, _ = roots(tmp_path)
    external = tmp_path / "source.txt"
    external.write_text("shared inode")
    os.link(external, config / "linked")
    with pytest.raises(ScopeDeniedError):
        _scan(config, max_files=50, max_bytes=100000, max_file_bytes=50000)
