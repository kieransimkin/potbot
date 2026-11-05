"""Own one release-verified canonical Syn relay for a fixed Agent cohort.

This adapter handles public CLI documents only. Syn owns membership checks,
encryption, signatures, replay checks, routing and native credential access.
"""

from __future__ import annotations

import os
import secrets
import select
import socket
import stat
import struct
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from potbot.domain.errors import ScopeDeniedError, StateMismatchError

from .agent import AgentSession, SynAgentDeployment
from .commands import SynPublicCommands
from .contracts import canonical
from .store import _directory, _write_new


class AttachedAgentSession:
    """Hold the exact native socket peer of an already-running enrolled Agent."""

    def __init__(self, endpoint: Path, owner_uid: int) -> None:
        entry = endpoint.lstat()
        if os.getuid() != 0 or entry.st_uid != owner_uid or not stat.S_ISSOCK(entry.st_mode):
            raise ScopeDeniedError("Native Agent endpoint has unsafe ownership")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(4)
            channel.connect(str(endpoint))
            pid, uid, gid = struct.unpack(
                "iII", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            if (
                pid <= 1
                or (uid, gid) != (owner_uid, owner_uid)
                or not os.path.samestat(entry, endpoint.lstat())
            ):
                raise ScopeDeniedError("Native Agent endpoint peer changed during attachment")
            self.pid = pid
            self._fd = os.pidfd_open(pid)

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    @property
    def healthy(self) -> bool:
        if self._fd < 0:
            return False
        wait = select.poll()
        wait.register(self._fd, select.POLLIN)
        return not wait.poll(0)

    def observe(self, proof: dict[str, Any], *, owner_uid: int) -> dict[str, Any]:
        path = Path("/proc") / str(self.pid)
        if (
            not self.healthy
            or b"-m\x00syn.agent\x00serve\x00" not in (path / "cmdline").read_bytes()
        ):
            raise StateMismatchError("Attached process is not a live native Syn Agent")
        result = AgentSession.measure(path, proof, owner_uid=owner_uid)
        if not self.healthy:
            raise StateMismatchError("Native Agent exited during attachment")
        return result


@dataclass(frozen=True)
class LiveSynParticipant:
    deployment: SynAgentDeployment
    session: AgentSession | AttachedAgentSession
    python: Path
    proof: dict[str, Any]
    observation: dict[str, Any]

    @property
    def selected_hash(self) -> str:
        return str(self.proof["status"]["group_certificate"]["body"]["node_public_key_hash"])


class SynPrivateMesh(SynPublicCommands):
    """Operator-only lifecycle; it never grants task/Cog authority."""

    def __init__(
        self,
        root: Path,
        *,
        workspace_id: UUID,
        participants: Mapping[UUID, tuple[SynAgentDeployment, AgentSession | AttachedAgentSession]],
    ) -> None:
        if (
            os.name != "posix"
            or getattr(os, "getuid", lambda: -1)() != 0
            or type(workspace_id) is not UUID
            or workspace_id.int == 0
            or not 1 <= len(participants) <= 8
        ):
            raise ScopeDeniedError("Private Syn mesh needs a native privileged workspace owner")
        _directory(root, create=True)
        self.root, self.workspace_id = root, workspace_id
        self._participants: dict[UUID, LiveSynParticipant] = {}
        self._locks: dict[UUID, threading.Lock] = {}
        self._process: subprocess.Popen[bytes] | None = None
        self._closed = False
        self._started = False
        certificates: list[dict[str, Any]] = []
        expected: tuple[Any, ...] | None = None
        for agent_id, (deployment, session) in participants.items():
            if (
                type(agent_id) is not UUID
                or not isinstance(deployment, SynAgentDeployment)
                or not isinstance(session, (AgentSession, AttachedAgentSession))
                or deployment.agent_id != agent_id
                or not session.healthy
            ):
                raise ScopeDeniedError(
                    "Private mesh requires independently enrolled owned Agent sessions"
                )
            installed, python = deployment._installation()
            runtime = installed["observed"]["runtime"]
            if (
                "syn.private-pubsub/1" not in runtime["api_profiles"]
                or "authenticated-private-pubsub" not in runtime["capabilities"]
            ):
                raise ScopeDeniedError(
                    "Select an official Syn release with private pub-sub contracts"
                )
            identity = (
                deployment.expected_operator,
                deployment.network_id,
                deployment.group_id,
                deployment.group_epoch,
                deployment.policy.manifest_sha256,
            )
            if expected is not None and identity != expected:
                raise ScopeDeniedError(
                    "Private mesh cannot mix groups, operators, epochs or release pins"
                )
            expected = identity
            proof = deployment.attach()
            observed = session.observe(proof, owner_uid=deployment.uid)
            frozen = LiveSynParticipant(deployment, session, python, proof, observed)
            if any(
                item.selected_hash == frozen.selected_hash for item in self._participants.values()
            ):
                raise ScopeDeniedError(
                    "Private mesh requires distinct independently selected node identities"
                )
            self._participants[agent_id] = frozen
            self._locks[agent_id] = threading.Lock()
            certificates.append(proof["status"]["group_certificate"])
        assert expected is not None
        self.scope: dict[str, Any] = {
            "profile": "syn.private-pubsub-scope/1",
            "workspace_id": str(workspace_id),
            "session_id": secrets.token_hex(16),
            "operator": expected[0],
            "network_id": expected[1],
            "group_id": str(expected[2]),
            "group_epoch": expected[3],
            "members": certificates,
        }

    @property
    def agents(self) -> tuple[UUID, ...]:
        return tuple(self._participants)

    @property
    def healthy(self) -> bool:
        return bool(
            self._started
            and not self._closed
            and self._process is not None
            and self._process.poll() is None
            and all(item.session.healthy for item in self._participants.values())
        )

    def participant(self, agent_id: UUID) -> LiveSynParticipant:
        if agent_id not in self._participants:
            raise ScopeDeniedError("Agent is outside the operator-selected native Syn cohort")
        return self._participants[agent_id]

    def provenance(self, agent_id: UUID) -> dict[str, Any]:
        """Public installation/identity pins for the existing execution journal."""
        selected = self.participant(agent_id)
        policy = selected.deployment.policy
        return {
            "profile": "potbot.syn-cog-native-binding/1",
            "workspace_id": str(self.workspace_id),
            "agent_id": str(agent_id),
            "session_id": self.scope["session_id"],
            "node": selected.selected_hash,
            "boot_id": selected.proof["status"]["boot_id"],
            "manifest_sha256": policy.manifest_sha256,
            "release_policy_sha256": policy.fingerprint,
            "source_commit": policy.value["source_commit"],
            "version": policy.value["version"],
            "owner_uid": selected.deployment.uid,
        }

    def call(self, agent_id: UUID, operation: str, payload: object = None) -> dict[str, Any]:
        if not self.healthy or operation not in {"status", "publish", "receive"}:
            raise StateMismatchError(
                "Private Syn mesh is unavailable or the operation is unsupported"
            )
        selected = self.participant(agent_id)
        body = selected.proof["status"]
        lock = self._locks[agent_id]
        if not lock.acquire(timeout=6):
            raise StateMismatchError("Native Syn command capacity deadline exceeded")
        try:
            path = self.root / ("public-request-" + uuid4().hex + ".json")
            _write_new(
                path, canonical({"scope": self.scope, "operation": operation, "payload": payload})
            )
            try:
                value = self._public_command(
                    selected.python,
                    [
                        "-m",
                        "syn_client.pubsub_client",
                        "--socket",
                        str(selected.deployment.root / agent_id.hex / "home/syn/agent.sock"),
                        "--owner-uid",
                        str(selected.deployment.uid),
                        "--expected-key",
                        selected.selected_hash,
                        "--configuration-sha256",
                        body["configuration_sha256"],
                        "--expected-boot",
                        body["boot_id"],
                        "--request",
                        str(path),
                    ],
                    timeout=6,
                )
            finally:
                path.unlink()
        finally:
            lock.release()
        if (
            value["reply"]["workspace_id"] != str(self.workspace_id)
            or value["reply"]["session_id"] != self.scope["session_id"]
            or value["reply"]["boot_id"] != body["boot_id"]
            or value["reply"]["node"] != selected.selected_hash
        ):
            raise StateMismatchError(
                "Verified Syn reply belongs to another selected native participant"
            )
        return value

    def probe(self, agent_id: UUID, *, verify_installation: bool = False) -> dict[str, Any]:
        selected = self.participant(agent_id)
        if verify_installation:
            proof = selected.deployment.attach()
            if proof["status"]["boot_id"] != selected.proof["status"]["boot_id"]:
                raise StateMismatchError(
                    "Native Agent restarted; reconcile its admitted mesh session"
                )
            selected.session.observe(proof, owner_uid=selected.deployment.uid)
        value = self.call(agent_id, "status")
        if value["reply"]["result"].get("cog_execution_authorized") is not False:
            raise StateMismatchError("Native Syn readiness cannot grant Cog authority")
        return value

    @contextmanager
    def running(self) -> Iterator[SynPrivateMesh]:
        if self._started or self._closed:
            raise StateMismatchError("A private Syn mesh lifetime is one-use")
        endpoints = [
            {
                "node": selected.selected_hash,
                "owner_uid": selected.deployment.uid,
                "socket": str(selected.deployment.root / agent.hex / "home/syn/agent.sock"),
            }
            for agent, selected in self._participants.items()
        ]
        path = self.root / "relay.json"
        _write_new(
            path,
            canonical(
                {
                    "profile": "syn.private-relay-config/1",
                    "scope": self.scope,
                    "endpoints": endpoints,
                }
            ),
        )
        selected = next(iter(self._participants.values()))
        reader: AgentSession | None = None
        try:
            self._process = subprocess.Popen(
                [str(selected.python), "-I", "-B", "-m", "syn.mesh", "--config", str(path)],
                cwd=self.root,
                env=self._environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            reader = AgentSession(self._process)
            ready = reader.read(timeout=15)
            if (
                ready.get("profile") != "syn.private-relay-ready/1"
                or ready.get("session_id") != self.scope["session_id"]
                or ready.get("workspace_id") != str(self.workspace_id)
                or ready.get("nodes")
                != sorted(item.selected_hash for item in self._participants.values())
                or ready.get("plaintext_access") is not False
                or ready.get("cog_execution_authorized") is not False
            ):
                raise StateMismatchError(
                    "Canonical Syn relay did not prove the selected public cohort"
                )
            self._started = True
            deadline = time.monotonic() + 5
            for agent in self._participants:
                while True:
                    try:
                        self.probe(agent)
                        break
                    except StateMismatchError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.05)
            yield self
        finally:
            self._closed = True
            if reader is not None:
                if reader.healthy:
                    reader.process.terminate()
                reader.stop()
            # Keep the exclusive relay claim until its exact child has exited.
            path.unlink()
