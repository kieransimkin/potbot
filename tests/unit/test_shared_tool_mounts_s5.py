"""S5: host-issued per-tool write grants are required for installer mounts."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.domain.errors import ScopeDeniedError
from potbot.tools.agent_homes import AgentHome
from potbot.tools.shared_tool_mounts import ToolWriteGrant
from potbot.tools.shared_workspace_isolation import SharedWorkspaceRuntime
from potbot.tools.workspace_concurrency import AccessRequest, AccessResource, WorkspaceLeaseLedger
from potbot.tools.workspace_supervision import WorkspaceLeaseGuard, admission_digest


@pytest.fixture
def admission(tmp_path: Path):
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    workspace_home = tmp_path / "home"
    workspace_home.mkdir()
    alice = workspace_home / "alice"
    alice.mkdir()
    (alice / "Tools").symlink_to("/usr/local/tools")
    private = tmp_path / "private"
    private.mkdir()
    tools = tmp_path / "tools"
    (tools / "stemlab").mkdir(parents=True)
    os.chmod(tools / "stemlab", 0o2775)
    ledger = WorkspaceLeaseLedger(private / "leases.sqlite", workspace_home=workspace_home)
    workspace_id, actor_id = uuid4(), uuid4()
    image_digest = "a" * 64
    ledger.pin_image(workspace_id, digest=image_digest,
                     image_ref="ghcr.io/test/system@sha256:" + image_digest,
                     build_commit="c" * 40, platform="linux/amd64",
                     rootfs_path=rootfs, qualification_sha256="f" * 64)
    runtime = SharedWorkspaceRuntime(image_digest, rootfs, workspace_home, "f" * 64,
                                     shared_tools=tools)
    agent = AgentHome(workspace_id, actor_id, "alice", 1, 420, 69, "testing")
    request = AccessRequest(workspace_id, actor_id, "tool.install", "install-a", "write",
                            (AccessResource("shared_tool", "stemlab", "write"),), image_digest)
    guard = WorkspaceLeaseGuard(ledger, request)
    return runtime, agent, guard, alice, tools


def test_tool_write_mount_requires_live_matching_lease_and_agent(admission):
    runtime, agent, guard, alice, tools = admission
    grant = ToolWriteGrant("stemlab", agent, guard)
    with pytest.raises(ScopeDeniedError):
        grant.admit(runtime=runtime, cwd=alice, guest_uid=420, guest_gid=69)
    guard.start()
    try:
        assert grant.admit(runtime=runtime, cwd=alice, guest_uid=420, guest_gid=69) == tools / "stemlab"
        with pytest.raises(ScopeDeniedError):
            grant.admit(runtime=runtime, cwd=alice, guest_uid=421, guest_gid=69)
        with pytest.raises(ScopeDeniedError):
            grant.admit(runtime=runtime, cwd=tools, guest_uid=420, guest_gid=69)
    finally:
        guard.finish(evidence_sha256=admission_digest("test-grant"))
    with pytest.raises(ScopeDeniedError):
        grant.admit(runtime=runtime, cwd=alice, guest_uid=420, guest_gid=69)


def test_agent_symlink_and_provisioning_must_match(admission):
    runtime, agent, guard, alice, _ = admission
    grant = ToolWriteGrant("stemlab", agent, guard)
    guard.start()
    try:
        (alice / "Tools").unlink()
        (alice / "Tools").symlink_to("/tmp/other")
        with pytest.raises(ScopeDeniedError):
            grant.admit(runtime=runtime, cwd=alice, guest_uid=420, guest_gid=69)
    finally:
        guard.finish(evidence_sha256=admission_digest("mismatch"))
