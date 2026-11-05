"""Local, credential-checked handoff of one native Syn cohort to a Cog worker."""

from __future__ import annotations

import os
import re
import socket
import stat
import struct
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.domain.errors import DomainError, ScopeDeniedError, StateMismatchError
from potbot.syn_release.contracts import DOCUMENT_LIMIT, canonical, document
from potbot.syn_release.mesh import SynPrivateMesh
from potbot.tools.syn_worker_nodes import HEARTBEAT_TYPE

_LIMIT = DOCUMENT_LIMIT
_TIMEOUT = 75


def root_path(path: Path) -> os.stat_result:
    """Resolve only administrator-controlled paths, without symlinks."""
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ScopeDeniedError("Native Syn dispatcher path is not canonical")
    for parent in path.parents:
        info = parent.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ScopeDeniedError("Native Syn dispatcher parent is not administrator-owned")
    return path.lstat()


def _peer(channel: socket.socket) -> tuple[int, int, int]:
    return struct.unpack("iII", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))


def _send(channel: socket.socket, value: dict[str, Any]) -> None:
    raw = canonical(value)
    if len(raw) > _LIMIT or channel.send(raw) != len(raw):
        raise StateMismatchError("Native Syn dispatcher packet exceeded its bound")


def _read(channel: socket.socket) -> dict[str, Any]:
    data, _, flags, _ = channel.recvmsg(_LIMIT)
    if not data or flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC):
        raise StateMismatchError("Native Syn dispatcher closed or truncated a packet")
    return document(data, canonical_only=True)


@dataclass(frozen=True, slots=True)
class MeshParticipant:
    selected_hash: str
    proof: dict[str, Any]


class NativeMeshClient:
    """A single authenticated worker lifetime. A lost connection is never reattached."""

    def __init__(
        self,
        path: Path,
        workspace: UUID,
        *,
        manifest_sha256: str,
        release_key_sha256: str,
        group_id: UUID,
        group_epoch: int,
        agents: frozenset[UUID],
    ) -> None:
        info = root_path(path)
        if info.st_uid != 0 or not stat.S_ISSOCK(info.st_mode):
            raise ScopeDeniedError("Native Syn dispatcher socket is not administrator-owned")
        self.workspace_id = workspace
        self._closed = False
        self._lock = threading.Lock()
        self._channel = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            self._channel.settimeout(_TIMEOUT)
            self._channel.connect(str(path))
            if _peer(self._channel)[1] != 0:
                raise ScopeDeniedError("Native Syn dispatcher peer is not the administrator")
            response = self._exchange({"op": "attach", "workspace_id": str(workspace)})
            if (
                set(response) != {"scope", "authority", "agents"}
                or response["authority"] != release_key_sha256
                or response["scope"]["workspace_id"] != str(workspace)
                or response["scope"]["group_id"] != str(group_id)
                or response["scope"]["group_epoch"] != group_epoch
                or set(response["agents"]) != {str(agent) for agent in agents}
                or any(
                    row["manifest_sha256"] != manifest_sha256
                    or row["agent_id"] != agent
                    or row["workspace_id"] != str(workspace)
                    or row["session_id"] != response["scope"]["session_id"]
                    for agent, row in response["agents"].items()
                )
            ):
                raise ScopeDeniedError("Native Syn dispatcher does not match its configured cohort")
            self.scope: dict[str, Any] = response["scope"]
            self._agents: dict[UUID, dict[str, Any]] = {
                UUID(agent): row for agent, row in response["agents"].items()
            }
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._closed = True
        self._channel.close()

    def _exchange(self, request: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise StateMismatchError("Native Syn dispatcher connection is closed")
            try:
                _send(self._channel, request)
                reply = _read(self._channel)
                if set(reply) == {"error"}:
                    raise ScopeDeniedError("Native Syn dispatcher refused the operation")
                if set(reply) != {"value"} or not isinstance(reply["value"], dict):
                    raise StateMismatchError("Native Syn dispatcher reply was invalid")
                return reply["value"]
            except ScopeDeniedError:
                raise
            except (OSError, ValueError, DomainError) as exc:
                self.close()
                raise StateMismatchError("Native Syn dispatcher connection was lost") from exc

    @property
    def agents(self) -> tuple[UUID, ...]:
        return tuple(self._agents)

    @property
    def healthy(self) -> bool:
        return not self._closed

    def provenance(self, agent: UUID) -> dict[str, Any]:
        if agent not in self._agents:
            raise ScopeDeniedError("Agent is outside the pinned native cohort")
        return dict(self._agents[agent])

    def participant(self, agent: UUID) -> MeshParticipant:
        row = self.provenance(agent)
        return MeshParticipant(row["node"], {"status": {"boot_id": row["boot_id"]}})

    def probe(self, agent: UUID, *, verify_installation: bool = False) -> dict[str, Any]:
        return self._exchange({"op": "probe", "agent": str(agent), "verify": verify_installation})

    def call(self, agent: UUID, operation: str, payload: object = None) -> dict[str, Any]:
        return self._exchange(
            {
                "op": operation,
                "agent": str(agent),
                **({"payload": payload} if operation == "publish" else {}),
            }
        )


def _dispatch(mesh: SynPrivateMesh, request: dict[str, Any]) -> dict[str, Any]:
    op = request.get("op")
    agent = UUID(request["agent"])
    if (
        op == "probe"
        and set(request) == {"op", "agent", "verify"}
        and type(request["verify"]) is bool
    ):
        return mesh.probe(agent, verify_installation=request["verify"])
    if op == "receive" and set(request) == {"op", "agent"}:
        return mesh.call(agent, op)
    if op == "publish" and set(request) == {"op", "agent", "payload"}:
        payload = request["payload"]
        if (
            isinstance(payload, dict)
            and set(payload) == {"topic", "value"}
            and payload["topic"] == HEARTBEAT_TYPE
            and isinstance(payload["value"], dict)
            and payload["value"].get("workspace_id") == str(mesh.workspace_id)
            and payload["value"].get("agent_id") == str(agent)
            and len(canonical(payload)) <= 4096
        ):
            return mesh.call(agent, op, payload)
    raise ScopeDeniedError("Native Syn dispatcher accepts only workspace heartbeats and readiness")


@contextmanager
def native_mesh_endpoint(
    mesh: SynPrivateMesh, path: Path, *, worker_uid: int, worker_gid: int
) -> Iterator[None]:
    """Expose only a running verified relay, with at most one queue consumer."""
    if os.getuid() != 0 or worker_uid <= 0 or worker_gid <= 0 or not mesh.healthy:
        raise ScopeDeniedError("Native Syn dispatcher needs the privileged verified cohort owner")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", path.name):
        raise ScopeDeniedError("Invalid native dispatcher socket name")
    directory = root_path(path.parent)
    if directory.st_uid != 0 or not stat.S_ISDIR(directory.st_mode) or directory.st_mode & 0o022:
        raise ScopeDeniedError("Native dispatcher socket needs a protected directory")
    authorities = {
        mesh.participant(agent).deployment.policy.value["release_key_sha256"]
        for agent in mesh.agents
    }
    if len(authorities) != 1:
        raise ScopeDeniedError("Native dispatcher cannot mix release authorities")
    authority = authorities.pop()
    stop = threading.Event()
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
        listener.bind(str(path))
        owned = path.lstat()
        os.chown(path, 0, worker_gid)
        path.chmod(0o660)
        listener.listen(1)
        listener.settimeout(0.25)

        def serve() -> None:
            while not stop.is_set() and mesh.healthy:
                try:
                    channel, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                with channel:
                    if _peer(channel)[1:] != (worker_uid, worker_gid):
                        continue
                    channel.settimeout(0.25)
                    attached = False
                    while not stop.is_set() and mesh.healthy:
                        try:
                            request = _read(channel)
                        except TimeoutError:
                            if attached:
                                continue
                            break
                        except (OSError, DomainError, ValueError):
                            break
                        try:
                            if not attached:
                                if request != {
                                    "op": "attach",
                                    "workspace_id": str(mesh.workspace_id),
                                }:
                                    raise ScopeDeniedError("Native dispatcher workspace mismatch")
                                answer: dict[str, Any] = {
                                    "scope": mesh.scope,
                                    "authority": authority,
                                    "agents": {
                                        str(agent): mesh.provenance(agent) for agent in mesh.agents
                                    },
                                }
                                attached = True
                            else:
                                answer = _dispatch(mesh, request)
                            _send(channel, {"value": answer})
                        except (KeyError, TypeError, ValueError, DomainError):
                            try:
                                _send(channel, {"error": "scope_denied"})
                            except (OSError, DomainError):
                                break
                            if not attached:
                                break
                        except OSError:
                            break

        thread = threading.Thread(target=serve, name="potbot-native-syn-dispatcher", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=_TIMEOUT)
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                path.unlink()
            if thread.is_alive():
                raise StateMismatchError("Native Syn dispatcher did not stop")
