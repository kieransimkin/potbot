"""Tests do not imply OS sandbox qualification: real bwrap/WSL gates are separate."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from potbot.context.model import ROLES
from potbot.context.projection import AgentContextProjection, ContextProjection
from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.tools import shared_workspace_isolation as isolation
from potbot.tools.shared_workspace_isolation import (
    SharedWorkspaceBubblewrapBackend,
    SharedWorkspaceRuntime,
    wsl_guest_preflight,
)


def test_wsl2_requires_explicit_disablement_and_no_host_mounts() -> None:
    config = """[automount]\nenabled=false\nmountFsTab=false\n[interop]\nenabled=false\nappendWindowsPath=false\n"""
    assert not wsl_guest_preflight(
        kernel_release="6.6.87.2-microsoft-standard-WSL2",
        config_text=config,
        mountinfo="1 0 0:1 / / rw - ext4 /dev/sda rw",
    )
    assert "Windows shared filesystem" in " ".join(
        wsl_guest_preflight(
            kernel_release="6.6.87.2-microsoft-standard-WSL2",
            config_text=config,
            mountinfo="21 1 0:99 /C:/ /mnt/c rw - 9p C: rw",
        )
    )
    assert wsl_guest_preflight(
        kernel_release="4.4.0-Microsoft",
        config_text=config,
        mountinfo="",
    ) == ("WSL2 Linux kernel required",)
    assert (
        len(
            wsl_guest_preflight(
                kernel_release="6.6.87.2-microsoft-standard-WSL2",
                config_text=None,
                mountinfo="",
            )
        )
        == 1
    )
    assert not wsl_guest_preflight(
        kernel_release="6.8.0-generic",
        config_text=None,
        mountinfo="",
    )


def test_scoped_worker_defers_only_the_wsl_driver_share_to_its_launcher() -> None:
    config = "[automount]\nenabled=false\nmountFsTab=false\n[interop]\nenabled=false\nappendWindowsPath=false\n"
    driver = "21 1 0:99 / /usr/lib/wsl/drivers rw - 9p drivers rw"
    host = "22 1 0:99 /C:/ /mnt/c rw - 9p C: rw"
    options = {"kernel_release": "6.6.87.2-microsoft-standard-WSL2", "config_text": config}
    assert wsl_guest_preflight(**options, mountinfo=driver)
    assert not wsl_guest_preflight(**options, mountinfo=driver, scoped_worker=True)
    assert wsl_guest_preflight(**options, mountinfo=f"{driver}\n{host}", scoped_worker=True)


def test_bubblewrap_rootfs_prepares_readonly_root_and_scoped_writes(
    tmp_path: Path,
    seccomp_descriptors,
) -> None:
    rootfs = tmp_path / "images" / "a"
    home = tmp_path / "workspaces" / "a" / "home"
    staging = tmp_path / "ai-staging"
    library = tmp_path / "global-models"
    tools = tmp_path / "shared-tools"
    for path in (
        rootfs / "bin",
        rootfs / "home",
        rootfs / "proc",
        rootfs / "dev",
        rootfs / "usr",
        rootfs / "var/ai",
        rootfs / "opt/potbot/models",
        home / ".tmp",
        home / "projects",
        staging,
        library,
        tools,
        rootfs / "usr/local/tools",
    ):
        path.mkdir(parents=True, exist_ok=True)
    (rootfs / "bin" / "bash").write_bytes(b"binary-fixture")
    (rootfs / "tmp").symlink_to("/home/.tmp")
    for name in ("bwrap", "prlimit"):
        f = tmp_path / name
        f.write_bytes(name.encode())
        f.chmod(0o755)
    runtime = SharedWorkspaceRuntime(
        "a" * 64,
        rootfs,
        home,
        "b" * 64,
        model_staging=staging,
        shared_models=library,
        shared_tools=tools,
    )
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

    with (
        patch("potbot.tools.shared_workspace_isolation.platform.system", return_value="Linux"),
        patch("potbot.tools.shared_workspace_isolation.platform.machine", return_value="x86_64"),
        patch("potbot.tools.shared_workspace_isolation.Path.read_text", new=read_text),
        patch(
            "potbot.tools.shared_workspace_isolation._seccomp_fd", side_effect=seccomp_descriptors
        ),
    ):
        kwargs = dict(
            argv=("/bin/bash",),
            workspace_root=home,
            cwd=home / "projects",
            read_only_roots=(),
            writable_roots=(),
            sandbox_root=home,
            status_fd=64,
            memory_bytes=1_000_000_000,
            cpu_seconds=30,
            max_file_bytes=10_000_000,
            max_open_files=256,
        )
        launch = bwrap.prepare(**kwargs)
        args = list(launch.argv)
        assert args[args.index("--ro-bind") + 1 : args.index("--ro-bind") + 3] == [str(rootfs), "/"]
        assert args[args.index("--bind") + 1 : args.index("--bind") + 3] == [str(home), "/home"]
        assert args[args.index("--chdir") + 1] == "/home/projects"
        positions = [i for i, value in enumerate(args) if value == "--bind"]
        assert [args[i + 1 : i + 3] for i in positions] == [
            [str(home), "/home"],
            [str(staging), "/var/ai"],
        ]
        assert [str(tools), "/usr/local/tools"] in [
            args[i + 1 : i + 3] for i, value in enumerate(args) if value == "--ro-bind"
        ]
        assert [str(library), "/opt/potbot/models"] in [
            args[i + 1 : i + 3] for i, value in enumerate(args) if value == "--ro-bind"
        ]
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
            SharedWorkspaceBubblewrapBackend(
                SharedWorkspaceRuntime("a" * 64, rootfs, home, "b" * 64),
                executable=tmp_path / "bwrap",
                prlimit=tmp_path / "prlimit",
            ).prepare(**kwargs)
        (rootfs / "tmp").unlink()
        with pytest.raises(CapabilityUnavailableError):
            bwrap.prepare(**kwargs)


def test_runtime_digest_required(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError):
        SharedWorkspaceRuntime("invalid", tmp_path, tmp_path, "a" * 64)


def test_shared_tool_executable_requires_operator_hash_and_rejects_symlinks(
    tmp_path: Path,
    seccomp_descriptors,
) -> None:
    """Pinned tool bytes are a prerequisite, not TOCTOU-resistant OS attestation."""
    root = tmp_path / "rootfs"
    home = tmp_path / "home"
    ai = tmp_path / "ai"
    models = tmp_path / "models"
    tools = tmp_path / "tools"
    for directory in (
        root / "bin",
        root / "home",
        root / "proc",
        root / "dev",
        root / "var/ai",
        root / "opt/potbot/models",
        root / "usr/local/tools",
        home / ".tmp",
        home / "alice",
        ai,
        models,
        tools / "stemlab/environment/bin",
    ):
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
    runtime = SharedWorkspaceRuntime(
        "a" * 64,
        root,
        home,
        "b" * 64,
        model_staging=ai,
        shared_models=models,
        shared_tools=tools,
        tool_entrypoints=((argv, digest),),
    )
    wrapper = SharedWorkspaceBubblewrapBackend(
        runtime, executable=tmp_path / "bwrap", prlimit=tmp_path / "prlimit"
    )
    real_read = Path.read_text

    def read_text(path: Path, *a: object, **kw: object) -> str:
        if str(path) == "/proc/sys/kernel/osrelease":
            return "6.8.0-generic"
        if str(path) == "/proc/self/mountinfo":
            return ""
        return real_read(path, *a, **kw)

    kwargs = dict(
        argv=(argv,),
        workspace_root=home,
        cwd=home / "alice",
        read_only_roots=(),
        writable_roots=(),
        sandbox_root=home,
        status_fd=64,
        memory_bytes=1_000_000_000,
        cpu_seconds=30,
        max_file_bytes=10_000_000,
        max_open_files=256,
    )
    with (
        patch("potbot.tools.shared_workspace_isolation.platform.system", return_value="Linux"),
        patch("potbot.tools.shared_workspace_isolation.platform.machine", return_value="x86_64"),
        patch("potbot.tools.shared_workspace_isolation.Path.read_text", new=read_text),
        patch(
            "potbot.tools.shared_workspace_isolation._seccomp_fd", side_effect=seccomp_descriptors
        ),
    ):
        prepared = wrapper.prepare(**kwargs)
        assert prepared.argv[-1] == argv
        assert any("/usr/local/tools/stemlab/environment/bin" in part for part in prepared.argv)
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
            SharedWorkspaceRuntime(
                "a" * 64,
                root,
                home,
                "b" * 64,
                model_staging=ai,
                shared_models=models,
                shared_tools=tools,
            ),
            executable=tmp_path / "bwrap",
            prlimit=tmp_path / "prlimit",
        )
        with pytest.raises(ScopeDeniedError):
            unregistered.prepare(**kwargs)


@pytest.fixture
def seccomp_descriptors(tmp_path: Path):
    """Real owned descriptor stand-ins; never load a filter or launch a guest."""
    owned = []
    source = tmp_path / "descriptor-fixture"
    source.write_bytes(b"not a seccomp program; prepare-only unit fixture")

    def allocate():
        fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC)
        info = os.fstat(fd)
        owned.append((fd, info.st_dev, info.st_ino))
        return fd

    try:
        yield allocate
    finally:
        for fd, device, inode in owned:
            try:
                info = os.fstat(fd)
            except OSError:
                continue  # Production failure cleanup already closed this FD.
            if (info.st_dev, info.st_ino) == (device, inode):
                os.close(fd)


@pytest.fixture
def context_launch(tmp_path: Path, monkeypatch, seccomp_descriptors):
    if os.name != "posix" or not Path("/proc/self/mountinfo").is_file():
        pytest.skip("Linux launch-builder filesystem contracts")
    image, home = tmp_path / "image", tmp_path / "home"
    staging, models, tools = (tmp_path / name for name in ("staging", "models", "tools"))
    for directory in (
        image / "bin",
        image / "home",
        image / "proc",
        image / "dev",
        image / "var/ai",
        image / "opt/potbot/models",
        image / "usr/local/tools",
        home / ".tmp",
        home / "alice",
        home / "bob",
        staging,
        models,
        tools,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (image / "tmp").symlink_to("/home/.tmp")
    (image / "bin/bash").write_bytes(b"never executed")
    for role in ROLES:
        for directory in (image, home / "alice", home / "bob"):
            (directory / role).write_bytes(b"")
            (directory / role).chmod(0o444)
    for name in ("bwrap", "prlimit"):
        (tmp_path / name).write_bytes(b"never executed")
        (tmp_path / name).chmod(0o700)
    workspace = uuid4()

    def projection(name):
        directory = tmp_path / name
        directory.mkdir(mode=0o700)
        hashes = []
        for role in ROLES:
            data = (name + " " + role).encode()
            (directory / role).write_bytes(data)
            (directory / role).chmod(0o444)
            hashes.append(hashlib.sha256(data).hexdigest())
        return ContextProjection(workspace, directory, (1, 1, 1), tuple(hashes))

    root_context = projection("root-context")
    agent_context = AgentContextProjection(uuid4(), "alice", projection("agent-context"))
    runtime = SharedWorkspaceRuntime(
        "a" * 64,
        image,
        home,
        "b" * 64,
        model_staging=staging,
        shared_models=models,
        shared_tools=tools,
    )
    monkeypatch.setattr(isolation, "runtime_attachment", lambda runtime: None)
    monkeypatch.setattr(isolation, "_seccomp_fd", seccomp_descriptors)
    monkeypatch.setattr(isolation.platform, "system", lambda: "Linux")
    monkeypatch.setattr(isolation.platform, "machine", lambda: "x86_64")

    def prepare(*, root=False, agent=False, selected=None, cwd=None):
        selected = selected or replace(
            runtime,
            context_projection=root_context if root else None,
            agent_context_projections=(agent_context,) if agent else (),
        )
        backend = SharedWorkspaceBubblewrapBackend(
            selected,
            executable=tmp_path / "bwrap",
            prlimit=tmp_path / "prlimit",
        )
        return backend.prepare(
            argv=("/bin/bash",),
            workspace_root=home,
            cwd=cwd or home / "alice",
            read_only_roots=(),
            writable_roots=(),
            sandbox_root=home,
            status_fd=64,
            memory_bytes=1024**3,
            cpu_seconds=30,
            max_file_bytes=1048576,
            max_open_files=256,
        )

    return prepare, runtime, root_context, agent_context


@pytest.mark.parametrize("root,agent", [(False, False), (True, False), (False, True), (True, True)])
def test_optional_context_mounts_are_independent_complete_readonly_triples(
    context_launch, root, agent
):
    prepare, _, root_context, agent_context = context_launch
    launch = prepare(root=root, agent=agent)
    binds = [
        launch.argv[i + 1 : i + 3] for i, token in enumerate(launch.argv) if token == "--ro-bind"
    ]
    actual = {dest: source for source, dest in binds if dest.endswith(ROLES)}
    expected = {}
    if root:
        expected.update({"/" + role: str(root_context.directory / role) for role in ROLES})
    if agent:
        expected.update(
            {
                "/home/alice/" + role: str(agent_context.projection.directory / role)
                for role in ROLES
            }
        )
    assert actual == expected
    writable = [launch.argv[i + 2] for i, token in enumerate(launch.argv) if token == "--bind"]
    assert writable == ["/home", "/var/ai"]
    assert not any(dest.startswith("/home/bob/") for _, dest in binds)
    assert "--unshare-all" in launch.argv and "--seccomp" in launch.argv
    assert len(launch.identity) == 64


@pytest.mark.parametrize("target", ["root", "agent"])
@pytest.mark.parametrize("count", [0, 1, 2, 4])
def test_configured_context_cannot_be_silently_omitted_or_truncated(
    context_launch, monkeypatch, target, count
):
    prepare, _, root_context, agent_context = context_launch
    selected = root_context if target == "root" else agent_context
    paths = tuple(root_context.directory / ROLES[i % 3] for i in range(count))
    monkeypatch.setattr(type(selected), "inspect", lambda *a, **kw: paths)
    with patch.object(isolation, "_seccomp_fd") as allocate:
        with pytest.raises(InvalidInputError, match="all three documents"):
            prepare(root=target == "root", agent=target == "agent")
        allocate.assert_not_called()


@pytest.mark.parametrize("target", ["root", "agent"])
@pytest.mark.parametrize("damage", ["missing", "changed", "symlink", "writable", "hardlink"])
def test_optional_mount_fix_keeps_real_projection_byte_and_filesystem_checks(
    context_launch, target, damage
):
    prepare, _, root_context, agent_context = context_launch
    context = root_context if target == "root" else agent_context.projection
    source = context.directory / "MODEL.md"
    if damage == "missing":
        source.unlink()
    elif damage == "changed":
        source.chmod(0o644)
        source.write_bytes(b"modified after capture")
        source.chmod(0o444)
    elif damage == "symlink":
        source.unlink()
        source.symlink_to(context.directory / "AGENTS.md")
    elif damage == "writable":
        source.chmod(0o644)
    else:
        os.link(source, context.directory / "alias")
    with pytest.raises((ScopeDeniedError, StateMismatchError)):
        prepare(root=target == "root", agent=target == "agent")


def test_missing_agent_binding_and_foreign_workspace_remain_denied(context_launch):
    prepare, runtime, root_context, agent_context = context_launch
    with pytest.raises(ScopeDeniedError, match="missing operator context"):
        prepare(agent=True, cwd=runtime.home / "bob")
    foreign = replace(
        agent_context, projection=replace(agent_context.projection, workspace_id=uuid4())
    )
    with pytest.raises(ScopeDeniedError, match="another workspace"):
        prepare(
            selected=replace(
                runtime, context_projection=root_context, agent_context_projections=(foreign,)
            )
        )


def test_failure_after_filter_allocation_closes_owned_descriptor(context_launch, monkeypatch):
    prepare, *_ = context_launch
    descriptors = []
    allocate = isolation._seccomp_fd

    def tracked():
        fd = allocate()
        descriptors.append(fd)
        return fd

    def fail(_):
        raise RuntimeError("injected identity hash failure")

    monkeypatch.setattr(isolation, "_seccomp_fd", tracked)
    monkeypatch.setattr(isolation, "content_hash", fail)
    with pytest.raises(RuntimeError, match="injected identity"):
        prepare()
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
