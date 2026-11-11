"""Installer tests for the private Syn Context route."""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.context import installed_syn_route as routes
from potbot.domain.errors import CapabilityUnavailableError, ScopeDeniedError, StateMismatchError
from potbot.syn_release import system_service
from potbot.syn_release.system_service import supervisor_arguments

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.getuid() == 0,
    reason="Requires an unprivileged native Linux service account",
)


@pytest.fixture
def short_root():
    with TemporaryDirectory(prefix="pcr-", dir="/tmp") as directory:
        yield Path(directory)


def test_route_provisioning_pins_one_workspace_and_requires_compatible_policy(short_root):
    deployment = SimpleNamespace(
        root=short_root,
        store=SimpleNamespace(root=short_root / "store"),
        policy=SimpleNamespace(value={"required_profiles": []}),
        _installation=lambda: None,
        _registered=lambda: None,
    )
    workspace = uuid4()
    with pytest.raises(CapabilityUnavailableError, match="pin directed"):
        routes.provision_context_route(deployment, workspace)
    deployment.policy.value["required_profiles"] = [routes.PROFILE]
    path = routes.provision_context_route(deployment, workspace)
    raw = json.loads(path.read_bytes())
    assert raw == {
        "method": "context.dispatch",
        "profile": "syn.local-rpc-route/1",
        "service_socket": str(short_root / "context-rpc.sock"),
        "service_uid": os.getuid(),
        "workspace_id": str(workspace),
    }
    assert path.stat().st_mode & 0o077 == 0
    assert routes.provision_context_route(deployment, workspace) == path
    with pytest.raises(ScopeDeniedError, match="another workspace"):
        routes.provision_context_route(deployment, uuid4())
    policy = short_root / "policy.json"
    policy.write_bytes(b"{}")
    policy.chmod(0o600)
    assert "--context-rpc" not in supervisor_arguments(deployment, policy)
    assert supervisor_arguments(deployment, policy, context_rpc=True)[-1] == "--context-rpc"

    deployment.policy.value["required_profiles"].append(routes.ROUTE_TABLE_PROFILE)
    table = routes.provision_context_route(deployment, workspace)
    assert table != path and json.loads(path.read_bytes()) == raw
    assert routes.operator_route_path(deployment) == table
    assert json.loads(table.read_bytes()) == {
        "profile": routes.ROUTE_TABLE_PROFILE,
        "routes": [
            {key: value for key, value in raw.items() if key != "profile"},
            {
                "method": "browser.command",
                "service_socket": str(short_root / "browser-rpc.sock"),
                "service_uid": os.getuid(),
                "workspace_id": str(workspace),
            },
        ],
    }


def test_private_endpoint_does_not_replace_foreign_entry_and_recovers_stale_socket(short_root):
    endpoint = short_root / "rpc.sock"
    endpoint.write_bytes(b"retained")
    with pytest.raises(ScopeDeniedError, match="unsafe"), routes._listener(endpoint):
        pytest.fail("Accepted an unrelated file")
    assert endpoint.read_bytes() == b"retained"
    endpoint.unlink()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as previous:
        previous.bind(str(endpoint))
        endpoint.chmod(0o600)
        previous.listen()
        with pytest.raises(ScopeDeniedError, match="belongs"), routes._listener(endpoint):
            pytest.fail("Accepted a live endpoint")
        assert endpoint.is_socket()
    with routes._listener(endpoint) as listener:
        assert endpoint.stat().st_mode & 0o777 == 0o600
        assert listener.getsockname() == str(endpoint)
    assert not endpoint.exists()


def test_managed_unit_restores_previous_bytes_if_route_restart_fails(short_root, monkeypatch):
    import pwd

    monkeypatch.setattr(pwd, "getpwuid", lambda _: SimpleNamespace(pw_dir=str(short_root)))
    policy = short_root / "policy.json"
    policy.write_bytes(b"{}")
    policy.chmod(0o600)
    deployment = SimpleNamespace(
        root=short_root,
        store=SimpleNamespace(root=short_root / "packages"),
        policy=SimpleNamespace(value={"required_profiles": [routes.PROFILE]}),
        _installation=lambda: None,
        _registered=lambda: None,
    )
    routes.provision_context_route(deployment, uuid4())
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        return SimpleNamespace(returncode=1 if len(commands) == 4 else 0)

    monkeypatch.setattr(system_service.subprocess, "run", run)
    unit = Path(system_service.install_service(deployment, policy)["unit"])
    previous = unit.read_bytes()
    with pytest.raises(StateMismatchError, match="did not start"):
        system_service.install_service(deployment, policy, context_rpc=True)
    assert unit.read_bytes() == previous
    assert sum("restart" in argv for argv in commands) == 2
    system_service.install_service(deployment, policy, context_rpc=True)
    assert b"--context-rpc" in unit.read_bytes()
