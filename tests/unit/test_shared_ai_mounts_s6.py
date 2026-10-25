"""Guest mount-contract tests (not actual kernel isolation qualification)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from potbot.domain.errors import CapabilityUnavailableError, ScopeDeniedError
from potbot.tools.ai_config_history import AiConfigHistoryMonitor
from potbot.tools.shared_workspace_isolation import (
    SharedWorkspaceBubblewrapBackend,
    SharedWorkspaceRuntime,
)


def test_ai_mounts_need_live_operator_watcher_and_expose_read_only_past(tmp_path: Path) -> None:
    rootfs, home = tmp_path / "rootfs", tmp_path / "home"
    staging, library, tools = (tmp_path / name for name in ("ai-staging", "models", "tools"))
    config, past = tmp_path / "etc-ai", tmp_path / "etc-ai-history"
    for directory in (
        rootfs / "home", rootfs / "proc", rootfs / "dev", rootfs / "etc/ai",
        rootfs / "etc/ai-past", rootfs / "var/ai", rootfs / "opt/potbot/models",
        rootfs / "usr/local/tools", rootfs / "bin", home / ".tmp",
        home / "alice", staging, library, tools, config, past,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (rootfs / "tmp").symlink_to("/home/.tmp")
    (rootfs / "bin/bash").write_bytes(b"fixture")
    for name in ("bwrap", "prlimit"):
        binary = tmp_path / name
        binary.write_bytes(name.encode())
        binary.chmod(0o755)
    os.chmod(config, 0o2775)
    runtime = SharedWorkspaceRuntime(
        "a" * 64, rootfs, home, "b" * 64, model_staging=staging,
        shared_models=library, shared_tools=tools, ai_config_dir=config, ai_history_dir=past,
    )
    real_read = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if str(path) == "/proc/sys/kernel/osrelease":
            return "6.8.0-generic"
        if str(path) == "/proc/self/mountinfo":
            return ""
        return real_read(path, *args, **kwargs)

    options = dict(
        argv=("/bin/bash",), workspace_root=home, cwd=home / "alice",
        read_only_roots=(), writable_roots=(), sandbox_root=home, status_fd=50,
        memory_bytes=1_000_000_000, cpu_seconds=30, max_file_bytes=1000000,
        max_open_files=256,
    )
    with patch("potbot.tools.shared_workspace_isolation.platform.system", return_value="Linux"), \
         patch("potbot.tools.shared_workspace_isolation.platform.machine", return_value="x86_64"), \
         patch("potbot.tools.shared_workspace_isolation.Path.read_text", new=read_text), \
         patch("potbot.tools.shared_workspace_isolation._seccomp_fd", return_value=50):
        no_watcher = SharedWorkspaceBubblewrapBackend(
            runtime, executable=tmp_path / "bwrap", prlimit=tmp_path / "prlimit"
        )
        with pytest.raises(CapabilityUnavailableError):
            no_watcher.prepare(**options)
        with AiConfigHistoryMonitor(config_dir=config, past_dir=past) as watcher:
            backend = SharedWorkspaceBubblewrapBackend(
                runtime, executable=tmp_path / "bwrap", prlimit=tmp_path / "prlimit",
                ai_monitor=watcher,
            )
            launch = backend.prepare(**options)
            argv = list(launch.argv)
            writable = [argv[i + 1:i + 3] for i, value in enumerate(argv) if value == "--bind"]
            readonly = [argv[i + 1:i + 3] for i, value in enumerate(argv) if value == "--ro-bind"]
            assert [str(config), "/etc/ai"] in writable
            assert [str(past), "/etc/ai-past"] in readonly
            assert [str(tools), "/usr/local/tools"] in readonly
            assert [str(tools), "/usr/local/tools"] not in writable
            assert argv[argv.index("--setenv", argv.index("--clearenv")) + 2] == "/home/alice"
            assert "POTBOT_AI_HISTORY" in argv and "/etc/ai-past" in argv
            assert "ai-history-ro" in launch.identity or len(launch.identity) == 64
        with pytest.raises(CapabilityUnavailableError):
            backend.prepare(**options)


def test_partial_ai_mount_configuration_rejected(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    with pytest.raises(ScopeDeniedError):
        AiConfigHistoryMonitor(config_dir=config, past_dir=config)
