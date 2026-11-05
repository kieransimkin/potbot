"""Run a local native Syn Cog relay for a root-selected live Agent cohort."""

from __future__ import annotations

import argparse
import os
import signal
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import UUID

from potbot.domain.errors import DomainError, ScopeDeniedError, StateMismatchError
from potbot.syn_release.agent import SynAgentDeployment
from potbot.syn_release.contracts import Policy, document, fields
from potbot.syn_release.mesh import AttachedAgentSession, SynPrivateMesh
from potbot.syn_release.mesh_endpoint import native_mesh_endpoint
from potbot.syn_release.store import SynReleaseStore, private_read


@contextmanager
def configured_endpoint(manifest: Path) -> Iterator[SynPrivateMesh]:
    if os.getuid() != 0:
        raise ScopeDeniedError("Native Syn mesh service requires the privileged Agent owner")
    settings = fields(
        document(private_read(manifest)),
        {
            "profile",
            "workspace_id",
            "store",
            "policy",
            "agent_root",
            "relay_root",
            "socket",
            "worker_uid",
            "worker_gid",
            "group_id",
            "group_epoch",
            "agents",
        },
    )
    agents = settings["agents"]
    if (
        settings["profile"] != "potbot.native-syn-dispatcher/1"
        or not isinstance(agents, dict)
        or not 1 <= len(agents) <= 8
        or any(type(uid) is not int or uid <= 0 for uid in agents.values())
        or len(set(agents.values())) != len(agents)
        or type(settings["worker_uid"]) is not int
        or settings["worker_uid"] <= 0
        or type(settings["worker_gid"]) is not int
        or settings["worker_gid"] <= 0
        or settings["worker_uid"] in agents.values()
    ):
        raise ScopeDeniedError("Native Syn dispatcher needs a fixed independent Agent cohort")
    root, policy_path = Path(settings["agent_root"]), Path(settings["policy"])
    store = SynReleaseStore(Path(settings["store"]))
    policy = Policy.parse(private_read(policy_path))
    participants: dict[UUID, tuple[SynAgentDeployment, AttachedAgentSession]] = {}
    with ExitStack() as resources:
        for identifier, uid in agents.items():
            agent = UUID(identifier)
            if str(agent) != identifier:
                raise ScopeDeniedError("Native Syn dispatcher Agent ID is not canonical")
            entry = root / agent.hex
            registration = document(private_read(entry / "deployment.json"), canonical_only=True)
            deployment = SynAgentDeployment(
                store,
                policy,
                root,
                agent_id=agent,
                uid=uid,
                expected_operator=registration["expected_operator"],
                network_id=registration["network_id"],
                group_id=UUID(settings["group_id"]),
                group_epoch=settings["group_epoch"],
                device_profile=registration["device_profile"],
                policy_path=policy_path,
            )
            attached = AttachedAgentSession(entry / "home/syn/agent.sock", uid)
            resources.callback(attached.close)
            participants[agent] = (deployment, attached)
        mesh = SynPrivateMesh(
            Path(settings["relay_root"]),
            workspace_id=UUID(settings["workspace_id"]),
            participants=participants,
        )
        with (
            mesh.running(),
            native_mesh_endpoint(
                mesh,
                Path(settings["socket"]),
                worker_uid=settings["worker_uid"],
                worker_gid=settings["worker_gid"],
            ),
        ):
            yield mesh


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    try:
        with configured_endpoint(args.manifest) as mesh:
            while not stop.wait(0.2):
                if not mesh.healthy:
                    raise StateMismatchError("Native Syn Agent or relay exited")
        return 0
    except (OSError, ValueError, KeyError, TypeError, DomainError):
        print("Native Syn dispatcher refused its configured cohort", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
