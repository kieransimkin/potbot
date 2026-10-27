"""S40 private X11 mount tests. No Xpra process or external site is launched."""
from __future__ import annotations

import os
import re
import socket
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.tools.agent_xpra_displays import AgentXpraDisplayPool, _private_directory
from potbot.tools.isolation import IsolationLaunch


def test_operator_display_root_rejects_shared_or_group_writable_directory(tmp_path: Path):
    allowed = tmp_path / "operator"
    allowed.mkdir(mode=0o700)
    allowed.chmod(0o700)
    assert _private_directory(allowed) == allowed.resolve()
    allowed.chmod(0o770)
    with pytest.raises(ScopeDeniedError):
        _private_directory(allowed)
    allowed.chmod(0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(allowed, target_is_directory=True)
    with pytest.raises(ScopeDeniedError):
        _private_directory(alias)


class FakeServer:
    def __init__(self, ws, path, readonly_mode):
        assert readonly_mode is True
        self.workspace_id, self.sandbox_root = ws, path
        self.display = ":" + str(410 + int(path.name[-2:], 16) % 100)
        self.running = False
        self.stopped = False

    def start(self):
        self.running = True

    def environment(self):
        if not self.running:
            raise StateMismatchError("display went away")
        return {"DISPLAY": self.display}

    def stop(self):
        self.running = False
        self.stopped = True


def test_operator_displays_are_independent_and_retire_only_after_worker_stop(tmp_path: Path):
    root = tmp_path / "operator"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    pool = object.__new__(AgentXpraDisplayPool)
    pool.root, pool.maximum, pool.server_factory = root, 2, FakeServer
    pool.workspace_id, pool.principal_id = uuid4(), uuid4()
    pool._lock = threading.RLock()
    pool._displays = {}
    agent_one, agent_two, agent_three = uuid4(), uuid4(), uuid4()
    pool._agent = lambda agent: SimpleNamespace(directory=agent.hex)
    first = pool.acquire(agent_one)
    second = pool.acquire(agent_two)
    assert first.server is not second.server
    assert first.server.sandbox_root != second.server.sandbox_root
    assert first.server.readonly_mode if hasattr(first.server, "readonly_mode") else True
    assert pool.acquire(agent_one) is first
    with pytest.raises(StateMismatchError):
        pool.acquire(agent_three)
    with pytest.raises(StateMismatchError):
        pool.retire(agent_one, process_stopped=False)
    assert first.server.running
    pool.retire(agent_one, process_stopped=True)
    assert first.server.stopped
    assert pool.acquire(agent_three).server.sandbox_root != second.server.sandbox_root
    pool.stop_all()
    assert not pool._displays


def test_mount_hides_other_homes_and_transfers_only_sealed_cookie(tmp_path: Path, monkeypatch):
    home = tmp_path / "workspace" / "home"
    agent_name, other_name = "agent-one", "agent-two"
    (home / agent_name).mkdir(parents=True)
    (home / other_name).mkdir(parents=True)
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    config = tmp_path / "etc-ai"
    config.mkdir()
    host_display = tmp_path / "operator"
    host_display.mkdir(mode=0o700)
    host_display.chmod(0o700)
    cookie = host_display / "Xauthority"
    cookie.write_bytes(b"private-authority-token")
    cookie.chmod(0o600)
    listener = socket.socket(socket.AF_UNIX)
    socket_path = str(host_display / "X473")
    listener.bind(socket_path)
    listener.listen(1)
    status_fd = os.open(os.devnull, os.O_WRONLY)
    agent_id = uuid4()

    class Qualified:
        def prepare(self, **kwargs):
            assert kwargs["guest_uid"] == 420 and kwargs["guest_gid"] == 69
            assert kwargs["cwd"] == home / agent_name
            sec_fd = os.open(os.devnull, os.O_RDONLY)
            args = (
                "prlimit", "--", "bwrap", "--unshare-all", "--ro-bind", str(rootfs), "/",
                "--bind", str(home), "/home", "--bind", str(staging), "/var/ai",
                "--ro-bind", str(tools), "/usr/local/tools", "--bind", str(config), "/etc/ai",
                "--proc", "/proc", "--dev", "/dev", "--clearenv",
                "--setenv", "HOME", f"/home/{agent_name}", "--chdir", f"/home/{agent_name}",
                "--seccomp", str(sec_fd), "--json-status-fd", str(status_fd), "--", "/bin/bash",
            )
            return IsolationLaunch(args, (sec_fd, status_fd), "a"*64)

    pool = object.__new__(AgentXpraDisplayPool)
    pool.runtime = SimpleNamespace(home=home, model_staging=staging, ai_config_dir=config)
    pool.workspace_id, pool.browser_gateway = uuid4(), None
    pool.backend = Qualified()
    pool.root = host_display
    server = SimpleNamespace(socket_path=Path(socket_path), auth_file=cookie,
           environment=lambda: {"DISPLAY": ":473"})
    pool._agent = lambda agent: SimpleNamespace(directory=agent_name, uid=420, gid=69)
    pool.acquire = lambda agent: SimpleNamespace(server=server, display=":473")
    # Test the actual kernel-file-descriptor arguments without installing a
    # seccomp filter in this pytest process.
    monkeypatch.setattr("potbot.tools.agent_xpra_displays._seccomp_fd",
                        lambda **kw: os.open(os.devnull, os.O_RDONLY))
    try:
        launch = pool.prepare(agent_id, argv=("/bin/bash",), status_fd=status_fd)
        a = list(launch.argv)
        assert ["--tmpfs", "/home"] == a[a.index("--tmpfs"):a.index("--tmpfs") + 2]
        assert ["--bind", str(home), "/home"] not in [a[i:i+3] for i in range(len(a)-2)]
        assert ["--bind", str(home / agent_name), f"/home/{agent_name}"] in [a[i:i+3] for i in range(len(a)-2)]
        assert f"/home/{other_name}" not in a
        assert ["--tmpfs", "/var/ai"] in [a[i:i+2] for i in range(len(a)-1)]
        assert ["--ro-bind", str(config), "/etc/ai"] in [a[i:i+3] for i in range(len(a)-2)]
        assert len(launch.pass_fds) == 3
        cookie_fd = int(a[a.index("--ro-bind-data") + 1])
        assert cookie_fd == launch.pass_fds[1]
        assert os.pread(cookie_fd, 4096, 0) == cookie.read_bytes()
        assert os.readlink(f"/proc/self/fd/{cookie_fd}").startswith("/memfd:")
        assert "private-authority-token" not in " ".join(a)
        assert re.fullmatch("[0-9a-f]{64}", launch.identity)
        for fd in launch.pass_fds:
            if fd != status_fd:
                os.close(fd)
    finally:
        os.close(status_fd)
        listener.close()


def test_unqualified_mount_plan_fails_before_fds_are_exposed(tmp_path: Path, monkeypatch):
    # Fake image no longer provides a private-network bwrap policy: reject.
    pool = object.__new__(AgentXpraDisplayPool)
    home = tmp_path / "home"
    (home / "agent").mkdir(parents=True)
    root = tmp_path / "operator"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    cookie = root / "Xauthority"
    cookie.write_bytes(b"cookie")
    cookie.chmod(0o600)
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(root / "X500"))
    pool.runtime = SimpleNamespace(home=home, model_staging=None, ai_config_dir=None)
    pool.workspace_id, pool.browser_gateway = uuid4(), None
    pool.root = root
    pool._agent = lambda _: SimpleNamespace(directory="agent", uid=420, gid=69)
    pool.acquire = lambda _: SimpleNamespace(server=SimpleNamespace(
       socket_path=root/"X500", auth_file=cookie,
       environment=lambda: {"DISPLAY": ":500"}), display=":500")
    class Broken:
        def prepare(self, **kwargs):
            fd = os.open(os.devnull, os.O_RDONLY)
            return IsolationLaunch(("bwrap", "--bind", str(home), "/home",
                                    "--seccomp", str(fd)), (fd, kwargs["status_fd"]), "b"*64)
    pool.backend = Broken()
    out = os.open(os.devnull, os.O_WRONLY)
    try:
        with pytest.raises(Exception, match="structure"):
            pool.prepare(uuid4(), argv=("/bin/bash",), status_fd=out)
    finally:
        os.close(out)
        sock.close()


def test_supervisor_adapter_rejects_foreign_agent_and_mount_overrides(tmp_path: Path):
    from potbot.tools.agent_xpra_displays import AgentXpraSandboxBackend
    home = tmp_path / "home"
    (home / "agent").mkdir(parents=True)
    pool = SimpleNamespace(
        runtime=SimpleNamespace(home=home),
        _agent=lambda x: SimpleNamespace(directory="agent", uid=420, gid=69),
        prepare=lambda x, **kw: IsolationLaunch(("bwrap",), (kw["status_fd"],), "f"*64),
    )
    backend = object.__new__(AgentXpraSandboxBackend)
    backend.pool = pool
    backend.runtime = pool.runtime
    backend.agent_id = uuid4()
    options = dict(argv=("/usr/bin/python3",), workspace_root=home, sandbox_root=home,
                   cwd=home/"agent", read_only_roots=(), writable_roots=(),
                   guest_uid=420, guest_gid=69, status_fd=3,
                   memory_bytes=1024**3, cpu_seconds=15, max_file_bytes=1024**2,
                   max_open_files=128)
    assert backend.prepare(**options).identity == "f"*64
    for mutation in ({"guest_uid": 421}, {"writable_roots": (home,)},
                     {"ipc_profile": "private-loopback-v1"}, {"cwd": home}):
        with pytest.raises(ScopeDeniedError):
            backend.prepare(**{**options, **mutation})
