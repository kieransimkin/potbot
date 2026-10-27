"""S15 + S40: only a real registered agent can own a private GUI audit."""
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
from potbot.domain.errors import ScopeDeniedError
from potbot.tools.agent_homes import AgentHomeRegistry
from potbot.tools.gui_resource_audit import GuiAuditFactory


def test_agent_identity_must_be_verified(monkeypatch, tmp_path):
    from potbot.tools import gui_resource_audit as module
    for name in ("rootfs", "home", "audit", "cgroup"):
        (tmp_path / name).mkdir()
    for name in ("strace", "join"):
        (tmp_path / name).write_bytes(b"bin")
    class Config:
        def __init__(self):
            for k in ("audit_root", "cgroup_root", "strace", "cgroup_join"):
                setattr(self, k, tmp_path / k.removesuffix("_root"))
    monkeypatch.setattr(module, "WorkerMonitorConfig", Config)
    runtime=SimpleNamespace(rootfs=tmp_path/"rootfs", home=tmp_path/"home",
        model_staging=None, shared_models=None, shared_tools=None,
        ai_config_dir=None, ai_history_dir=None)
    workspace, principal, agent=uuid4(), uuid4(), uuid4()
    seen=[]
    class FakeMonitor:
        def __init__(self, *args, **kwargs):
            seen.append((args, kwargs))
    audit=GuiAuditFactory(config=Config(), runtime=runtime,
        workspace_id=workspace, principal_id=principal, session_factory=FakeMonitor)
    registry=AgentHomeRegistry.__new__(AgentHomeRegistry)
    monkeypatch.setattr(AgentHomeRegistry, "lookup", lambda self, ws, uid: (
        SimpleNamespace(workspace_id=ws, agent_id=uid) if uid==agent else None))
    audit("a"*64, uuid4(), agent_id=agent, agent_registry=registry)
    assert seen[0][0][2] == agent and seen[0][1]["identity_kind"] == "agent"
    with pytest.raises(ScopeDeniedError):
        audit("a"*64, uuid4(), agent_id=uuid4(), agent_registry=registry)
    with pytest.raises(ScopeDeniedError):
        audit("a"*64, uuid4(), agent_id=agent, agent_registry=object())
