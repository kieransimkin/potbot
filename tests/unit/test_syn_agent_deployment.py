"""Agent deployment fails before native effects when release authority is absent.

Real UID, keyring, namespace, replay and restart qualification belongs to the
upstream two-node native gate, not these small public-controller regressions.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from potbot.domain.errors import ScopeDeniedError
from potbot.syn_release.agent import AgentSession, SynAgentDeployment
from potbot.syn_release.contracts import Policy, canonical
from tests.unit.test_syn_release_installation import make_bundle


def test_installing_a_service_package_cannot_provision_an_agent(tmp_path):
    bundle = make_bundle()
    policy = Policy.parse(canonical(bundle.value))
    destination = tmp_path / "agents"
    with pytest.raises(ScopeDeniedError, match="explicit upstream Linux Agent"):
        SynAgentDeployment(SimpleNamespace(root=tmp_path / "packages"), policy, destination,
            agent_id=UUID("be31002b-b7a6-4e46-bd29-4e39fb04e31e"), uid=53102,
            expected_operator="12" * 32, network_id="fixture",
            group_id=UUID("4f672321-11ba-4405-843d-a5961a784bf6"), group_epoch=1,
            device_profile="syn.host-bound-virtual-device/1")
    assert not destination.exists()


def test_private_enrollment_pipe_never_becomes_an_arbitrary_command_runner():
    session = AgentSession(SimpleNamespace(stdin=None))
    for operation in ("exec", "sign", "export-key", "read-password", "delete-identity"):
        with pytest.raises(ScopeDeniedError, match="explicit Syn Agent"):
            session.exchange(operation)


def test_enrollment_frame_bound_is_checked_before_any_write():
    class RefuseWrites:
        def write(self, _):
            raise AssertionError("Oversized frame reached the child")
    session = AgentSession(SimpleNamespace(stdin=RefuseWrites()))
    with pytest.raises(ScopeDeniedError, match="bound"):
        session.exchange("offer", {"padding": "x" * 65536})
