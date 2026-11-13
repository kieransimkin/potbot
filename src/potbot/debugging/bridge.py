"""Expose a selected native Syn debugger Agent to one unprivileged service user."""

from __future__ import annotations

import argparse
import os
import re
import signal
import socket
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from potbot.debugging.syn_forwarding import MAX_BYTES, TOPIC, debugger_group_id
from potbot.domain.errors import DomainError, ScopeDeniedError, StateMismatchError
from potbot.syn_release.contracts import Policy, canonical, digest, document, fields
from potbot.syn_release.mesh_endpoint import _peer, _read, _send, root_path
from potbot.syn_release.pubsub_endpoint import SynPubSubEndpoint
from potbot.syn_release.store import SynReleaseStore, private_read
from potbot.tools.execution_contracts import identifier


def _boot(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{32}", value):
        raise ScopeDeniedError("Expected an exact Syn boot ID")
    return value


def _roster(scope: dict[str, Any], node: str, peer: str) -> None:
    if (
        node == peer
        or scope["group_id"] != str(debugger_group_id(scope["operator"]))
        or len(scope["members"]) != 2
        or {row["body"]["node_public_key_hash"] for row in scope["members"]} != {node, peer}
    ):
        raise ScopeDeniedError("The debugger requires a dedicated operator group")


class ServiceDebuggerEndpoint:
    """One lifetime on the administrator-owned local debugger bridge."""

    def __init__(self, path: Path) -> None:
        info = root_path(path)
        if info.st_uid != 0 or not stat.S_ISSOCK(info.st_mode):
            raise ScopeDeniedError("Debugger bridge socket must be administrator-owned")
        self._channel = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            self._channel.settimeout(10)
            self._channel.connect(str(path))
            if _peer(self._channel)[1] != 0 or not os.path.samestat(info, path.lstat()):
                raise ScopeDeniedError("Debugger bridge peer changed")
            hello = fields(
                self._exchange({"op": "attach"}),
                {
                    "scope",
                    "node",
                    "peer",
                    "peer_boot",
                    "manifest_sha256",
                },
            )
            self.node = digest(hello["node"])
            self.peer = digest(hello["peer"])
            self.peer_boot = _boot(hello["peer_boot"])
            self.manifest_sha256 = digest(hello["manifest_sha256"])
            self._scope = canonical(hello["scope"])
            _roster(self.scope, self.node, self.peer)
        except BaseException:
            self.close()
            raise

    @property
    def scope(self) -> dict[str, Any]:
        return document(self._scope)

    def close(self) -> None:
        self._channel.close()

    def _exchange(self, value: dict[str, Any]) -> dict[str, Any]:
        try:
            _send(self._channel, value)
            result = _read(self._channel)
            if set(result) != {"value"} or not isinstance(result["value"], dict):
                raise StateMismatchError("Debugger bridge refused the operation")
            return result["value"]
        except (OSError, DomainError):
            self.close()
            raise

    def call(self, operation: str, payload: object = None) -> dict[str, Any]:
        request: dict[str, Any] = {"op": operation}
        if operation == "publish":
            request["payload"] = payload
        return self._exchange(request)


def _bridge_call(
    endpoint: SynPubSubEndpoint, request: dict[str, Any], peer: str, role: str
) -> dict[str, Any]:
    if request == {"op": "receive"}:
        result = endpoint.call("receive")
        return {
            "messages": [
                item for item in result["messages"] if item["envelope"]["header"]["topic"] == TOPIC
            ]
        }
    message = fields(fields(request, {"op", "payload"})["payload"], {"topic", "value"})
    body = message["value"]
    if (
        request["op"] != "publish"
        or message["topic"] != TOPIC
        or not isinstance(body, dict)
        or len(canonical(message)) > MAX_BYTES
        or body.get("kind") != ("reply" if role == "target" else "request")
        or body.get("session") != endpoint.scope["session_id"]
        or body.get("to") != peer
    ):
        raise ScopeDeniedError("Debugger bridge publication differs from the selected route")
    return endpoint.call("publish", message)


@contextmanager
def debugger_bridge(
    endpoint: SynPubSubEndpoint,
    path: Path,
    *,
    peer: str,
    peer_boot: str,
    manifest_sha256: str,
    service_uid: int,
    service_gid: int,
    role: str,
) -> Iterator[None]:
    """Pass only verified debugger packets for one fixed Syn scope and OS identity."""
    if (
        os.getuid() != 0
        or type(service_uid) is not int
        or service_uid <= 0
        or type(service_gid) is not int
        or service_gid <= 0
        or role not in {"target", "caller"}
    ):
        raise ScopeDeniedError("Invalid debugger bridge role or service account")
    _roster(endpoint.scope, endpoint.node, digest(peer))
    hello = {
        "scope": endpoint.scope,
        "node": endpoint.node,
        "peer": peer,
        "peer_boot": _boot(peer_boot),
        "manifest_sha256": digest(manifest_sha256),
    }
    directory = root_path(path.parent)
    if directory.st_uid != 0 or directory.st_mode & 0o022 or not stat.S_ISDIR(directory.st_mode):
        raise ScopeDeniedError("Debugger bridge requires an administrator-owned directory")
    if endpoint.call("status").get("connected") is not True:
        raise StateMismatchError("Debugger Agent mesh is unavailable")
    stop = threading.Event()
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
        listener.bind(str(path))
        owned = path.lstat()
        try:
            os.chown(path, 0, service_gid)
            path.chmod(0o660)
            listener.listen(1)
            listener.settimeout(0.25)

            def serve() -> None:
                while not stop.is_set():
                    try:
                        channel, _ = listener.accept()
                    except TimeoutError:
                        continue
                    except OSError:
                        return
                    with channel:
                        if _peer(channel)[1:] != (service_uid, service_gid):
                            continue
                        channel.settimeout(0.25)
                        try:
                            if _read(channel) != {"op": "attach"}:
                                continue
                            _send(channel, {"value": hello})
                            while not stop.is_set():
                                try:
                                    request = _read(channel)
                                except TimeoutError:
                                    continue
                                _send(
                                    channel, {"value": _bridge_call(endpoint, request, peer, role)}
                                )
                        except (OSError, DomainError, ValueError, TypeError, KeyError):
                            continue

            thread = threading.Thread(target=serve, name="potbot-debugger-bridge", daemon=True)
            thread.start()
            try:
                yield
            finally:
                stop.set()
                thread.join(timeout=8)
                if thread.is_alive():
                    raise StateMismatchError("Debugger bridge did not stop")
        finally:
            if path.exists() and os.path.samestat(owned, path.lstat()):
                path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("registration", type=Path)
    args = parser.parse_args()
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    try:
        settings = fields(
            document(private_read(args.registration)),
            {
                "profile",
                "store",
                "policy",
                "bridge",
                "agent_socket",
                "agent_uid",
                "node",
                "boot",
                "agent_config_sha256",
                "scope",
                "peer",
                "peer_boot",
                "service_uid",
                "service_gid",
                "role",
            },
        )
        if settings["profile"] != "potbot.debugger-bridge/1":
            raise ScopeDeniedError("Unknown debugger bridge registration")
        policy = Policy.parse(private_read(Path(settings["policy"])))
        if policy.value["mode"] != "agent" or policy.value["version"] != "0.10.1":
            raise ScopeDeniedError("Debugger bridge requires the qualified Syn Agent release")
        store = SynReleaseStore(Path(settings["store"]))
        verified = store.verify(policy)
        identifier(settings["scope"]["workspace_id"])
        endpoint = SynPubSubEndpoint(
            args.registration.parent,
            python=store.root / "installs" / verified["directory"] / "venv/bin/python",
            socket_path=Path(settings["agent_socket"]),
            owner_uid=settings["agent_uid"],
            node=digest(settings["node"]),
            boot=_boot(settings["boot"]),
            configuration_sha256=digest(settings["agent_config_sha256"]),
            scope=settings["scope"],
        )
        with debugger_bridge(
            endpoint,
            Path(settings["bridge"]),
            peer=settings["peer"],
            peer_boot=settings["peer_boot"],
            manifest_sha256=policy.manifest_sha256,
            service_uid=settings["service_uid"],
            service_gid=settings["service_gid"],
            role=settings["role"],
        ):
            while not stop.wait(0.5):
                if endpoint.call("status").get("connected") is not True:
                    raise StateMismatchError("Debugger Agent mesh is unavailable")
        return 0
    except (OSError, DomainError, TypeError, ValueError, KeyError):
        print("Debugger bridge refused its configured session", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
