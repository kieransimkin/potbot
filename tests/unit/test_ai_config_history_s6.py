"""S6: durable reversible configuration changes and fail-closed watcher health."""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

import pytest

from potbot.domain.errors import CapabilityUnavailableError, ScopeDeniedError, StateMismatchError
from potbot.tools.ai_config_history import AiConfigHistoryMonitor, reconstruct_prior_snapshot

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="inotify requires Linux")


def test_revisions_capture_reverse_text_binary_delete_rename_and_modes(tmp_path: Path) -> None:
    config, past = tmp_path / "config", tmp_path / "past"
    config.mkdir()
    past.mkdir()
    (config / "settings.ini").write_text("version=1\n", encoding="utf-8")
    (config / "weights.bin").write_bytes(b"\x00\xfforiginal\n")
    (config / "gone.txt").write_text("please restore\n", encoding="utf-8")
    (config / "renamed.txt").write_text("keep bytes\n", encoding="utf-8")
    watcher = AiConfigHistoryMonitor(config_dir=config, past_dir=past, interval_seconds=5)
    watcher.start()
    try:
        (config / "settings.ini").write_text("version=2\n", encoding="utf-8")
        (config / "weights.bin").write_bytes(b"\x00\xffchanged\n")
        (config / "gone.txt").unlink()
        (config / "renamed.txt").rename(config / "new-name.txt")
        (config / "extras").mkdir()
        (config / "extras" / "script.sh").write_text("echo yay\n", encoding="utf-8")
        reverse = watcher.poll_once()
        assert reverse is not None
        assert reverse.sequence == 1
        assert reverse.before_digest != reverse.after_digest
        recorded = json.loads(reverse.inverse_path.read_text())
        changes = {entry["path"]: entry for entry in recorded["changes"]}
        assert "settings.ini" in changes and "weights.bin" in changes
        assert changes["gone.txt"]["after"] is None
        assert base64.b64decode(changes["gone.txt"]["before"]["data_base64"]) == b"please restore\n"
        assert changes["renamed.txt"]["after"] is None
        assert changes["new-name.txt"]["before"] is None
        assert changes["extras"]["before"] is None
        assert changes["extras/script.sh"]["before"] is None
        current = json.loads((past / "head.json").read_text())["snapshot"]
        old = reconstruct_prior_snapshot(current, recorded)
        assert old["settings.ini"]["data_base64"] == changes["settings.ini"]["before"]["data_base64"]
        assert "new-name.txt" not in old and "renamed.txt" in old
        assert base64.b64decode(changes["weights.bin"]["before"]["data_base64"]) == b"\x00\xfforiginal\n"
        human = reverse.text_diff_path.read_text()
        assert "-version=2" in human and "+version=1" in human
        assert "binary weights.bin" in human
        watcher.require_active(config_dir=config, past_dir=past)
        assert json.loads((past / "head.json").read_text())["digest"] == reverse.after_digest
    finally:
        watcher.stop()
    with pytest.raises(CapabilityUnavailableError):
        watcher.require_active()


def test_restart_rejects_unobserved_mutation(tmp_path: Path) -> None:
    config, past = tmp_path / "config", tmp_path / "past"
    config.mkdir()
    past.mkdir()
    (config / "a.txt").write_text("baseline")
    first = AiConfigHistoryMonitor(config_dir=config, past_dir=past)
    first.start()
    first.stop()
    (config / "a.txt").write_text("changed offline")
    restarted = AiConfigHistoryMonitor(config_dir=config, past_dir=past)
    with pytest.raises(StateMismatchError):
        restarted.start()
    with pytest.raises(CapabilityUnavailableError):
        restarted.require_active()


def test_symlink_dropped_or_special_file_fences_new_launch(tmp_path: Path) -> None:
    config, past = tmp_path / "config", tmp_path / "past"
    config.mkdir()
    past.mkdir()
    external = tmp_path / "elsewhere.txt"
    external.write_text("private")
    watcher = AiConfigHistoryMonitor(config_dir=config, past_dir=past, interval_seconds=5)
    watcher.start()
    try:
        (config / "link").symlink_to(external)
        with pytest.raises(ScopeDeniedError):
            watcher.poll_once()
        with pytest.raises(CapabilityUnavailableError):
            watcher.require_active()
        assert external.read_text() == "private"
    finally:
        watcher.stop()


def test_health_detects_modified_watcher_and_rejects_invalid_mounts(tmp_path: Path) -> None:
    config, past = tmp_path / "config", tmp_path / "past"
    config.mkdir()
    past.mkdir()
    with pytest.raises(ScopeDeniedError):
        AiConfigHistoryMonitor(config_dir=config, past_dir=config)
    watcher = AiConfigHistoryMonitor(config_dir=config, past_dir=past)
    watcher.start()
    try:
        with pytest.raises(ScopeDeniedError):
            watcher.require_active(config_dir=past, past_dir=past)
    finally:
        watcher.stop()


def test_monitor_catches_changes_without_ignoring_new_directory(tmp_path: Path) -> None:
    config, past = tmp_path / "config", tmp_path / "past"
    config.mkdir()
    past.mkdir()
    watcher = AiConfigHistoryMonitor(config_dir=config, past_dir=past, interval_seconds=0.05)
    watcher.start()
    try:
        (config / "newdir").mkdir()
        (config / "newdir" / "quick.txt").write_bytes(b"value")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if json.loads((past / "head.json").read_text())["snapshot"].get("newdir/quick.txt"):
                break
            time.sleep(0.05)
        assert json.loads((past / "head.json").read_text())["snapshot"]["newdir/quick.txt"]["sha256"]
        watcher.require_active()
    finally:
        watcher.stop()


def test_directory_deletion_and_discarded_watch_are_reconciled(tmp_path: Path) -> None:
    config, past = tmp_path / "config", tmp_path / "past"
    config.mkdir()
    past.mkdir()
    (config / "dir").mkdir()
    (config / "dir" / "a.txt").write_text("original")
    with AiConfigHistoryMonitor(config_dir=config, past_dir=past, interval_seconds=5) as watcher:
        (config / "dir" / "a.txt").unlink()
        (config / "dir").rmdir()
        recorded = watcher.poll_once()
        assert recorded is not None
        assert recorded.sequence == 1
        watcher.require_active()
        changes = {e["path"]: e for e in json.loads(recorded.inverse_path.read_text())["changes"]}
        assert changes["dir"]["after"] is None
        assert changes["dir/a.txt"]["after"] is None


def test_history_store_must_not_be_group_writable(tmp_path: Path) -> None:
    config, past = tmp_path / "config", tmp_path / "past"
    config.mkdir()
    past.mkdir(mode=0o777)
    past.chmod(0o775)
    with pytest.raises(ScopeDeniedError):
        AiConfigHistoryMonitor(config_dir=config, past_dir=past)


def test_history_directory_allows_only_one_operator(tmp_path: Path) -> None:
    config, past = tmp_path / "config", tmp_path / "past"
    config.mkdir()
    past.mkdir()
    first = AiConfigHistoryMonitor(config_dir=config, past_dir=past)
    second = AiConfigHistoryMonitor(config_dir=config, past_dir=past)
    with first:
        with pytest.raises(StateMismatchError):
            second.start()
    # A third process is allowed to recover the unchanged baseline after
    # the original monitor has relinquished its host lock.
    with AiConfigHistoryMonitor(config_dir=config, past_dir=past) as recovered:
        recovered.require_active()
