"""Tests do not imply OS sandbox qualification: real bwrap/WSL gates are separate."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from potbot.domain.errors import CapabilityUnavailableError, ScopeDeniedError
from potbot.tools.shared_workspace_isolation import (
    SharedWorkspaceBubblewrapBackend,
    SharedWorkspaceRuntime,
    wsl_guest_preflight,
)


def test_wsl2_requires_explicit_disablement_and_no_host_mounts() -> None:
    config = """[automount]\nenabled=false\nmountFsTab=false\n[interop]\nenabled=false\nappendWindowsPath=false\n"""
    assert not wsl_guest_preflight(
        kernel_release="6.6.87.2-microsoft-standard-WSL2", config_text=config,
        mountinfo="1 0 0:1 / / rw - ext4 /dev/sda rw",
    )
    assert "Windows shared filesystem" in " ".join(wsl_guest_preflight(
        kernel_release="6.6.87.2-microsoft-standard-WSL2", config_text=config,
        mountinfo="21 1 0:99 /C:/ /mnt/c rw - 9p C: rw",
    ))
    assert wsl_guest_preflight(
        kernel_release="4.4.0-Microsoft", config_text=config, mountinfo="",
    ) == ("WSL2 Linux kernel required",)
    assert len(wsl_guest_preflight(
        kernel_release="6.6.87.2-microsoft-standard-WSL2", config_text=None,
        mountinfo="",
    )) == 1
    assert not wsl_guest_preflight(
        kernel_release="6.8.0-generic", config_text=None, mountinfo="",
    )


def test_bubblewrap_rootfs_prepares_readonly_root_and_scoped_writes(tmp_path: Path) -> None:
    rootfs = tmp_path / "images" / "a"
    home = tmp_path / "workspaces" / "a" / "home"
    staging = tmp_path / "ai-staging"
    library = tmp_path / "global-models"
    tools = tmp_path / "shared-tools"
    for path in (rootfs / "bin", rootfs / "home", rootfs / "proc", rootfs / "dev",
                 rootfs / "usr", rootfs / "var/ai", rootfs / "opt/potbot/models",
                 home / ".tmp", home / "projects", staging, library, tools,
                 rootfs / "usr/local/tools"):
        path.mkdir(parents=True, exist_ok=True)
    (rootfs / "bin" / "bash").write_bytes(b"binary-fixture")
    (rootfs / "tmp").symlink_to("/home/.tmp")
    for name in ("bwrap", "prlimit"):
        f = tmp_path / name
        f.write_bytes(name.encode())
        f.chmod(0o755)
    runtime = SharedWorkspaceRuntime("a" * 64, rootfs, home, "b" * 64,
                                     model_staging=staging, shared_models=library,
                                     shared_tools=tools)
    bwrap = SharedWorkspaceBubblewrapBackend(
        runtime, executable=tmp_path / "bwrap", prlimit=tmp_path / "prlimit"
    )
    real_read = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if str(path) == "/proc/sys/kernel/osrelease":
            return "6.8.0-generic"
        if str(path) == "/proc/self/mountinfo":
            return ""
        return real_read(path, *args, **kwargs)

    with patch("potbot.tools.shared_workspace_isolation.platform.system", return_value="Linux"), \
         patch("potbot.tools.shared_workspace_isolation.platform.machine", return_value="x86_64"), \
         patch("potbot.tools.shared_workspace_isolation.Path.read_text", new=read_text), \
         patch("potbot.tools.shared_workspace_isolation._seccomp_fd", return_value=63):
        kwargs = dict(
            argv=("/bin/bash",), workspace_root=home, cwd=home / "projects",
            read_only_roots=(), writable_roots=(), sandbox_root=home, status_fd=64,
            memory_bytes=1_000_000_000, cpu_seconds=30,
            max_file_bytes=10_000_000, max_open_files=256,
        )
        launch = bwrap.prepare(**kwargs)
        args = list(launch.argv)
        assert args[args.index("--ro-bind") + 1: args.index("--ro-bind") + 3] == [str(rootfs), "/"]
        assert args[args.index("--bind") + 1:args.index("--bind") + 3] == [str(home), "/home"]
        assert args[args.index("--chdir") + 1] == "/home/projects"
        positions = [i for i, value in enumerate(args) if value == "--bind"]
        assert [args[i + 1:i + 3] for i in positions] == [
            [str(home), "/home"], [str(staging), "/var/ai"]]
        assert [str(tools), "/usr/local/tools"] in [
            args[i + 1:i + 3] for i, value in enumerate(args) if value == "--ro-bind"]
        assert [str(library), "/opt/potbot/models"] in [
            args[i + 1:i + 3] for i, value in enumerate(args) if value == "--ro-bind"]
        assert "HF_HOME" in args and "/var/ai/cache/huggingface" in args
        assert "OLLAMA_MODELS" in args
        assert "--unshare-all" in args and "--seccomp" in args
        assert "--share-net" not in args and "--tmpfs" not in args
        assert args[-1] == "/bin/bash"
        with pytest.raises(ScopeDeniedError):
            bwrap.prepare(**{**kwargs, "writable_roots": (home / "projects",)})
        with pytest.raises(ScopeDeniedError):
            bwrap.prepare(**{**kwargs, "cwd": rootfs})
        with pytest.raises(CapabilityUnavailableError):
            bwrap.prepare(**kwargs) if False else SharedWorkspaceBubblewrapBackend(
                SharedWorkspaceRuntime("a" * 64, rootfs, home, "b" * 64),
                executable=tmp_path / "bwrap", prlimit=tmp_path / "prlimit").prepare(**kwargs)
        (rootfs / "tmp").unlink()
        with pytest.raises(CapabilityUnavailableError):
            bwrap.prepare(**kwargs)


def test_runtime_digest_required(tmp_path: Path) -> None:
    from potbot.domain.errors import InvalidInputError
    with pytest.raises(InvalidInputError):
        SharedWorkspaceRuntime("invalid", tmp_path, tmp_path, "a" * 64)


def test_shared_tool_executable_requires_operator_hash_and_rejects_symlinks(tmp_path: Path) -> None:
    """Pinned tool bytes are a prerequisite, not TOCTOU-resistant OS attestation."""
    import hashlib
    from potbot.domain.errors import ScopeDeniedError

    root = tmp_path / "rootfs"
    home = tmp_path / "home"
    ai = tmp_path / "ai"
    models = tmp_path / "models"
    tools = tmp_path / "tools"
    for directory in (root / "bin", root / "home", root / "proc", root / "dev",
                      root / "var/ai", root / "opt/potbot/models", root / "usr/local/tools",
                      home / ".tmp", home / "alice", ai, models,
                      tools / "stemlab/environment/bin"):
        directory.mkdir(parents=True, exist_ok=True)
    (root / "bin/bash").write_bytes(b"bash")
    (root / "tmp").symlink_to("/home/.tmp")
    candidate = tools / "stemlab/environment/bin/stemlab"
    candidate.write_bytes(b"admitted fixture")
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    for name in ("bwrap", "prlimit"):
        file = tmp_path / name
        file.write_bytes(b"bin")
        file.chmod(0o755)
    argv = "/usr/local/tools/stemlab/environment/bin/stemlab"
    runtime = SharedWorkspaceRuntime("a" * 64, root, home, "b" * 64,
                                     model_staging=ai, shared_models=models, shared_tools=tools,
                                     tool_entrypoints=((argv, digest),))
    wrapper = SharedWorkspaceBubblewrapBackend(runtime, executable=tmp_path / "bwrap",
                                               prlimit=tmp_path / "prlimit")
    real_read = Path.read_text

    def read_text(path: Path, *a: object, **kw: object) -> str:
        if str(path) == "/proc/sys/kernel/osrelease":
            return "6.8.0-generic"
        if str(path) == "/proc/self/mountinfo":
            return ""
        return real_read(path, *a, **kw)

    kwargs = dict(argv=(argv,), workspace_root=home, cwd=home / "alice",
                  read_only_roots=(), writable_roots=(), sandbox_root=home,
                  status_fd=64, memory_bytes=1_000_000_000, cpu_seconds=30,
                  max_file_bytes=10_000_000, max_open_files=256)
    with patch("potbot.tools.shared_workspace_isolation.platform.system", return_value="Linux"), \
         patch("potbot.tools.shared_workspace_isolation.platform.machine", return_value="x86_64"), \
         patch("potbot.tools.shared_workspace_isolation.Path.read_text", new=read_text), \
         patch("potbot.tools.shared_workspace_isolation._seccomp_fd", return_value=63):
        prepared = wrapper.prepare(**kwargs)
        assert prepared.argv[-1] == argv
        assert any("/usr/local/tools/stemlab/environment/bin" in part
                   for part in prepared.argv)
        assert "/home/alice/.cache" in prepared.argv
        assert "PYTHONDONTWRITEBYTECODE" in prepared.argv
        candidate.write_bytes(b"changed")
        with pytest.raises(ScopeDeniedError):
            wrapper.prepare(**kwargs)
        candidate.unlink()
        candidate.symlink_to("/etc/hosts")
        with pytest.raises(ScopeDeniedError):
            wrapper.prepare(**kwargs)
        candidate.unlink()
        candidate.write_bytes(b"admitted fixture")
        unregistered = SharedWorkspaceBubblewrapBackend(
            SharedWorkspaceRuntime("a" * 64, root, home, "b" * 64,
                                   model_staging=ai, shared_models=models, shared_tools=tools),
            executable=tmp_path / "bwrap", prlimit=tmp_path / "prlimit")
        with pytest.raises(ScopeDeniedError):
            unregistered.prepare(**kwargs)
