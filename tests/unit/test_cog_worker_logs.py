"""Linux acceptance of the shared Cog worker log mount."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.domain.errors import ScopeDeniedError
from potbot.tools.agent_homes import AgentHomeRegistry
from potbot.tools.shared_workspace_isolation import (
    SharedWorkspaceBubblewrapBackend,
    SharedWorkspaceRuntime,
)
from potbot.tools.supervisor import ProcessRequest
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger

_PROBE = r"""
uid="$1"
marker="$2"
peer_path="$3"
peer_id="$4"
log="$POTBOT_AGENT_LOG_DIR/worker.log"
test "$POTBOT_AGENT_LOG_DIR" = /var/logs/ai
test "$(/bin/busybox id -u)" = "$uid"
test "$(/bin/busybox ls -A /var/logs)" = ai
test ! -e "$peer_path"
for target in "$peer_path" "/home/peer-$peer_id" "/var/logs/$peer_id/worker.log"; do
    if (printf 'foreign write\n' >> "$target") 2>/dev/null; then
        exit 70
    fi
done
printf '%s\n' "$marker" >> "$log"
if /bin/busybox ln "$log" "/home/leak-$marker" 2>/dev/null; then
    exit 71
fi
"""


@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("POTBOT_TEST_COG_LOGS") != "1",
    reason="requires an isolated Linux runner with Bubblewrap and static BusyBox",
)
def test_shared_cog_worker_logs_survive_restart_and_deny_peer_writes(tmp_path: Path) -> None:
    rootfs, home, staging, models, tools, private = (
        tmp_path / name for name in ("image", "home", "staging", "models", "tools", "private")
    )
    for path in (
        home / ".tmp",
        staging,
        models,
        tools,
        private,
        *(
            rootfs / name
            for name in (
                "home",
                "proc",
                "dev",
                "bin",
                "var/ai",
                "var/logs/ai",
                "opt/potbot/models",
                "usr/local/tools",
            )
        ),
    ):
        path.mkdir(parents=True)
    (rootfs / "tmp").symlink_to("/home/.tmp")
    shutil.copy2("/usr/bin/busybox", rootfs / "bin/busybox")

    workspace_id = uuid4()
    agent_ids = (uuid4(), uuid4())
    runtime = SharedWorkspaceRuntime(
        "a" * 64,
        rootfs,
        home,
        "b" * 64,
        model_staging=staging,
        shared_models=models,
        shared_tools=tools,
    )
    ledger_path = private / "leases.sqlite"
    ledger = WorkspaceLeaseLedger(ledger_path, workspace_home=home)
    ledger.pin_image(
        workspace_id,
        digest=runtime.image_digest,
        image_ref=f"ghcr.io/test/image@sha256:{runtime.image_digest}",
        build_commit="c" * 40,
        platform="linux/amd64",
        rootfs_path=rootfs,
        qualification_sha256=runtime.qualification_sha256,
    )
    homes = AgentHomeRegistry(ledger, workspace_name="log-test")
    for agent_id in agent_ids:
        homes.provision(workspace_id, agent_id)
    paths = tuple(private / "cog-logs" / workspace_id.hex / a.hex / "worker.log" for a in agent_ids)
    for agent_id, path in zip(agent_ids, paths, strict=True):
        (home / ("peer-" + agent_id.hex)).symlink_to(path)

    for boot in (1, 2):
        # Reopen the durable registration and construct a fresh launcher.
        ledger = WorkspaceLeaseLedger(ledger_path, workspace_home=home)
        homes = AgentHomeRegistry(ledger, workspace_name="log-test")
        backend = SharedWorkspaceBubblewrapBackend(runtime)
        for index, agent_id in enumerate(agent_ids):
            registration = homes.lookup(workspace_id, agent_id)
            assert registration is not None
            peer = 1 - index
            request = ProcessRequest(
                argv=(
                    "/bin/busybox",
                    "sh",
                    "-ec",
                    _PROBE,
                    "worker",
                    str(registration.uid),
                    f"{agent_id.hex}-{boot}",
                    str(paths[peer]),
                    agent_ids[peer].hex,
                ),
                cwd=home / registration.directory,
                workspace_root=home,
                guest_uid=registration.uid,
                guest_gid=registration.gid,
                telemetry_workspace_id=workspace_id,
                telemetry_agent_id=agent_id,
                telemetry_identity_kind="agent",
                cog_log_parent=ledger.path.parent,
            )
            status_read, status_write = os.pipe()
            launch = None
            try:
                launch = backend.prepare(
                    argv=request.argv,
                    workspace_root=home,
                    cwd=request.cwd,
                    read_only_roots=(),
                    writable_roots=(),
                    sandbox_root=home,
                    status_fd=status_write,
                    memory_bytes=request.memory_bytes,
                    cpu_seconds=10,
                    max_file_bytes=request.max_file_bytes,
                    max_open_files=request.max_open_files,
                    **request.shared_launch_options(backend),
                )
                result = subprocess.run(
                    launch.argv,
                    pass_fds=launch.pass_fds,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            finally:
                for fd in {status_read, status_write, *(launch.pass_fds if launch else ())}:
                    os.close(fd)
            assert result.returncode == 0, result.stderr
    for agent_id, path in zip(agent_ids, paths, strict=True):
        assert path.read_text().splitlines() == [f"{agent_id.hex}-1", f"{agent_id.hex}-2"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux log directory custody")
def test_shared_cog_log_scope_rejects_mount_overlap_and_symlinks(tmp_path: Path) -> None:
    from potbot.tools.shared_workspace_isolation import _prepare_cog_logs

    image, controller = tmp_path / "image", tmp_path / "controller"
    target = image / "var/logs/ai"
    target.mkdir(parents=True)
    controller.mkdir()
    workspace, agent = uuid4(), uuid4()
    scope = (controller, workspace, agent)
    with pytest.raises(ScopeDeniedError, match="overlaps a guest mount"):
        _prepare_cog_logs(image, scope, (controller,))

    partition = _prepare_cog_logs(image, scope, (image,))
    assert partition is not None
    partition.rmdir()
    partition.symlink_to(controller)
    with pytest.raises(ScopeDeniedError, match="private and controller-owned"):
        _prepare_cog_logs(image, scope, (image,))

    target.rmdir()
    assert _prepare_cog_logs(image, scope, (image,)) is None
    target.symlink_to(controller)
    with pytest.raises(ScopeDeniedError, match="destination is redirected"):
        _prepare_cog_logs(image, scope, (image,))
