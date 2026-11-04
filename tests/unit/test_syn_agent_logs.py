"""Linux acceptance for persistent logs in the native Agent sandbox."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.domain.errors import ScopeDeniedError
from potbot.syn_release.agent_host import agent_log_directory, provision
from potbot.syn_release.agent_sandbox import prepared_launch

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.getuid() != 0,
    reason="Linux native UID and permission tests require root",
)

_UIDS = (53102, 53103)
_LOG_PROBE = """#!/usr/bin/python3
import os
import sys
from pathlib import Path

uid, marker, foreign = sys.argv[-3:]
assert os.getuid() == os.getgid() == int(uid)
assert os.environ["POTBOT_AGENT_LOG_DIR"] == "/var/logs/ai"
assert not Path(foreign).exists()
with (Path(os.environ["POTBOT_AGENT_LOG_DIR"]) / "agent.log").open("a") as log:
    log.write(marker + "\\n")
"""


def _append(directory: Path, uid: int, marker: str) -> subprocess.CompletedProcess[str]:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import os,sys; "
                "fd=os.open('agent.log',os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600,"
                "dir_fd=int(sys.argv[1])); "
                "os.write(fd,sys.argv[2].encode()); os.close(fd)",
                str(fd),
                marker,
            ],
            user=uid,
            group=uid,
            extra_groups=[],
            pass_fds=(fd,),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    finally:
        os.close(fd)


def test_agent_log_directory_survives_new_process_and_denies_peer_uid(tmp_path: Path):
    directories = []
    for uid in _UIDS:
        parent = tmp_path / str(uid)
        parent.mkdir(mode=0o700)
        path = agent_log_directory(parent / "home", uid, uid, create=True)
        directories.append(path)
        assert _append(path, uid, "first").returncode == 0
        assert agent_log_directory(parent / "home", uid, uid, create=True) == path
        assert _append(path, uid, "second").returncode == 0
    for index, path in enumerate(directories):
        denied = _append(path, _UIDS[1 - index], "peer")
        assert denied.returncode != 0 and "PermissionError" in denied.stderr
        assert (path / "agent.log").read_text() == "firstsecond"

    directory = directories[0]
    directory.rename(directory.with_name("held-logs"))
    directory.symlink_to(directories[1], target_is_directory=True)
    with pytest.raises(ScopeDeniedError, match="Agent log directory"):
        agent_log_directory(directory.parent / "home", _UIDS[0], _UIDS[0], create=True)


@pytest.mark.skipif(
    os.environ.get("POTBOT_TEST_NATIVE_AGENT_LOGS") != "1",
    reason="requires an isolated Linux runner with disposable native Agent accounts",
)
def test_native_agent_log_mount_survives_sandbox_restart(tmp_path: Path):
    runtime = tmp_path / "probe-runtime"
    runtime.mkdir(mode=0o755)
    probe = runtime / "probe"
    probe.write_text(_LOG_PROBE)
    probe.chmod(0o555)
    root = tmp_path / "agents"
    ids = (uuid4(), uuid4())
    hosts = [
        provision(root, agent_id=agent_id, uid=uid)
        for agent_id, uid in zip(ids, _UIDS, strict=True)
    ]
    directories = [Path(host["home"]).with_name("logs") for host in hosts]
    # Simulate an existing registration created before this mount was introduced.
    directories[0].rmdir()

    for boot in (1, 2):
        for index, uid in enumerate(_UIDS):
            host = provision(root, agent_id=ids[index], uid=uid)
            assert host == hosts[index]
            with prepared_launch(
                host=host,
                python=probe,
                runtime_roots=(runtime,),
                arguments=(str(uid), f"{uid}/{boot}", str(directories[1 - index])),
            ) as launch:
                result = subprocess.run(
                    launch.argv,
                    pass_fds=launch.pass_fds,
                    capture_output=True,
                    text=True,
                    timeout=45,
                    check=False,
                )
            assert result.returncode == 0, result.stderr
    for index, uid in enumerate(_UIDS):
        directory = directories[index]
        denied = _append(directory, _UIDS[1 - index], "peer")
        assert denied.returncode != 0 and "PermissionError" in denied.stderr
        assert (directory / "agent.log").read_text().splitlines() == [f"{uid}/1", f"{uid}/2"]
