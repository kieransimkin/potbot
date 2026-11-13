"""Controller-side access to one pinned native Agent in an installed Syn mesh."""

from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from potbot.domain.errors import StateMismatchError
from potbot.syn_release.agent import SynAgentDeployment
from potbot.syn_release.commands import SynPublicCommands
from potbot.syn_release.contracts import canonical, document
from potbot.syn_release.store import _directory, _write_new


class SynPubSubEndpoint(SynPublicCommands):
    """Use the official SDK to verify each native socket exchange."""

    def __init__(
        self,
        root: Path,
        *,
        python: Path,
        socket_path: Path,
        owner_uid: int,
        node: str,
        boot: str,
        configuration_sha256: str,
        scope: dict[str, Any],
    ) -> None:
        _directory(root)
        self.root = root
        self._python, self._socket_path, self._uid = python, socket_path, owner_uid
        self.node, self.boot = node, boot
        self._configuration = configuration_sha256
        self._scope_bytes = canonical(scope)

    @property
    def scope(self) -> dict[str, Any]:
        return document(self._scope_bytes)

    @classmethod
    def from_deployment(
        cls, deployment: SynAgentDeployment, root: Path, *, scope: dict[str, Any]
    ) -> "SynPubSubEndpoint":
        _, python = deployment._installation()
        status = deployment.attach()["status"]
        certificate = status["group_certificate"]
        if certificate not in scope["members"]:
            raise StateMismatchError("Native Agent is outside the selected Syn group")
        return cls(
            root,
            python=python,
            socket_path=deployment.root / deployment.agent_id.hex / "home/syn/agent.sock",
            owner_uid=deployment.uid,
            node=certificate["body"]["node_public_key_hash"],
            boot=status["boot_id"],
            configuration_sha256=status["configuration_sha256"],
            scope=scope,
        )

    def call(self, operation: str, payload: object = None) -> dict[str, Any]:
        return cast(dict[str, Any], self.packet(operation, payload)["reply"]["result"])

    def packet(self, operation: str, payload: object = None) -> dict[str, Any]:
        if operation not in {"status", "publish", "receive"}:
            raise StateMismatchError("Unknown native Syn pub/sub operation")
        path = self.root / ("pubsub-" + uuid4().hex + ".json")
        _write_new(
            path, canonical({"scope": self.scope, "operation": operation, "payload": payload})
        )
        try:
            packet = self._public_command(
                self._python,
                [
                    "-m",
                    "syn_client.pubsub_client",
                    "--socket",
                    str(self._socket_path),
                    "--owner-uid",
                    str(self._uid),
                    "--expected-key",
                    self.node,
                    "--configuration-sha256",
                    self._configuration,
                    "--expected-boot",
                    self.boot,
                    "--request",
                    str(path),
                ],
                timeout=6,
            )
        finally:
            path.unlink()
        return packet
