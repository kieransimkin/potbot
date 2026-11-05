"""Provision a real, release-pinned Syn Agent; public/encrypted frames only.

Syn owns the private native prompt, keyring, selection and key transfer. Potbot
owns restricted OS resources, immutable package mounts and this process lease.
No Cog or task-execution authority follows from provisioning a node.
"""

from __future__ import annotations

import hashlib
import os
import selectors
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from potbot.domain.errors import ScopeDeniedError, StateMismatchError

from .agent_host import provision
from .agent_runtime import runtime_view
from .agent_sandbox import prepared_launch
from .commands import SynPublicCommands
from .contracts import Policy, canonical, digest, document
from .store import SynReleaseStore, _directory, _write_new, native_platform, private_read

GROUP_ROLLOVER_OPERATIONS = {"prepare-group-rollover", "apply-group-rollover"}


class AgentSession:
    """One owned namespace/session; the controlling TTY belongs to Syn only."""

    def __init__(self, process: subprocess.Popen[bytes], *, group_rollover: bool = False):
        self.process = process
        self.group_rollover = group_rollover
        self.buffer = bytearray()
        self.initial: dict[str, Any] | None = None

    @property
    def healthy(self) -> bool:
        return self.process.poll() is None

    def read(self, *, timeout: int = 120) -> dict[str, Any]:
        if self.process.stdout is None:
            raise StateMismatchError("Syn private session has no public response pipe")
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b"\n" not in self.buffer:
                if self.process.poll() is not None:
                    raise StateMismatchError("Syn private Agent session exited; deployment fenced")
                if time.monotonic() >= deadline:
                    raise StateMismatchError("Syn Agent response exceeded its deadline")
                if not selector.select(0.1):
                    continue
                part = os.read(self.process.stdout.fileno(), min(4096, 65538 - len(self.buffer)))
                if not part:
                    raise StateMismatchError("Syn Agent closed its public response pipe")
                self.buffer.extend(part)
                if len(self.buffer) > 65537:
                    raise StateMismatchError("Syn Agent public response exceeded its bound")
        line, _, remainder = self.buffer.partition(b"\n")
        self.buffer[:] = remainder
        return document(bytes(line), canonical_only=True)

    def exchange(self, operation: str, payload: object = None) -> dict[str, Any]:
        if operation not in {
            "offer",
            "choose",
            "prepare",
            "adopt",
            "adopt-group",
            "activate",
            "serve",
            "restart",
            "lock",
        } and not (self.group_rollover and operation in GROUP_ROLLOVER_OPERATIONS):
            raise ScopeDeniedError("Unknown explicit Syn Agent maintenance operation")
        raw = canonical({"operation": operation, "payload": payload})
        if len(raw) > 65536 or self.process.stdin is None:
            raise ScopeDeniedError("Syn Agent maintenance frame exceeds its bound")
        self.process.stdin.write(raw + b"\n")
        self.process.stdin.flush()
        return self.read(timeout=30)

    def stop(self) -> None:
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # This exact Popen child is the only process this controller owns.
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process.stdout is not None:
            self.process.stdout.close()

    def observe(self, proof: dict[str, Any], *, owner_uid: int) -> dict[str, Any]:
        """Measure only this owned process tree; never scan the host process list."""
        if os.name != "posix" or os.getuid() != 0 or self.process.poll() is not None:
            raise StateMismatchError(
                "Native Agent observation requires the live owned Linux session"
            )
        pending = [self.process.pid]
        visited: set[int] = set()
        matched: list[Path] = []
        while pending:
            pid = pending.pop()
            if pid in visited:
                continue
            visited.add(pid)
            if len(visited) > 64:
                raise StateMismatchError("Owned Agent process tree exceeded its bound")
            path = Path(f"/proc/{pid}")
            try:
                if b"-m\x00syn.agent\x00serve\x00" in (path / "cmdline").read_bytes():
                    matched.append(path)
                pending.extend(map(int, (path / f"task/{pid}/children").read_text().split()))
            except FileNotFoundError:
                continue
        if len(matched) != 1:
            raise StateMismatchError("Owned session has no unique real Syn Agent process")
        return self.measure(matched[0], proof, owner_uid=owner_uid)

    @staticmethod
    def measure(path: Path, proof: dict[str, Any], *, owner_uid: int) -> dict[str, Any]:
        """Inspect an exact live PID selected by a trusted host handle."""
        status = dict(row.split(":", 1) for row in (path / "status").read_text().splitlines())
        if (
            list(map(int, status["Uid"].split())) != [owner_uid] * 4
            or list(map(int, status["Gid"].split())) != [owner_uid] * 4
            or int(status["NSpid"].split()[-1]) != proof["status"]["pid"]
            or any(
                int(status[name].strip(), 16) != 0
                for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
            )
            or status["NoNewPrivs"].strip() != "1"
            or status["Seccomp"].strip() != "2"
        ):
            raise StateMismatchError(
                "Real Agent native UID, privileges or syscall filter differs from its admission profile"
            )
        maps = {tuple(map(int, row.split())) for row in (path / "uid_map").read_text().splitlines()}
        if maps != {(0, 0, 1), (owner_uid, owner_uid, 1)}:
            raise StateMismatchError(
                "Agent UID is not retained independently in the host namespace"
            )
        namespaces = {
            name: os.readlink(path / f"ns/{name}")
            for name in ("pid", "mnt", "net", "user", "ipc", "uts", "cgroup")
        }
        if any(value == os.readlink(f"/proc/self/ns/{name}") for name, value in namespaces.items()):
            raise StateMismatchError("Agent unexpectedly shares a controller namespace")
        limits = (path / "limits").read_text()
        if "1073741824" not in limits or "16777216" not in limits:
            raise StateMismatchError("Agent process limits differ from the runtime profile")
        return {
            "profile": "potbot.syn-agent-native-observation/1",
            "native_pid": int(path.name),
            "native_uid": owner_uid,
            "namespaces": namespaces,
            "capabilities_zero": True,
            "no_new_privileges": True,
            "seccomp_filter_active": True,
            "process_limits_measured": True,
        }


class SynAgentDeployment(SynPublicCommands):
    def __init__(
        self,
        store: SynReleaseStore,
        policy: Policy,
        root: Path,
        *,
        agent_id: UUID,
        uid: int,
        expected_operator: str,
        network_id: str,
        group_id: UUID,
        group_epoch: int,
        device_profile: str,
        policy_path: Path | None = None,
    ):
        selected = policy.value
        if (
            selected["component"] != "runtime"
            or selected["mode"] != "agent"
            or not {"syn.agent-local/1", "syn.agent-group/1"} <= set(selected["required_profiles"])
            or not {
                "authenticated-agent-ipc",
                "confidential-selected-key-adoption",
                "host-bound-virtual-device",
                "native-os-agent-custody",
                "native-os-group-custody",
                "retained-agent-identity",
            }
            <= set(selected["required_capabilities"])
        ):
            raise ScopeDeniedError(
                "Agent deployment requires explicit upstream Linux Agent contracts"
            )
        if os.name != "posix" or native_platform() != "linux-x86_64" or os.getuid() != 0:
            raise ScopeDeniedError(
                "Agent OS provisioning requires the privileged native deployment controller"
            )
        _directory(root, create=True)
        if root.is_relative_to(store.root) or store.root.is_relative_to(root):
            raise ScopeDeniedError(
                "Agent mutable state must be separate from immutable Syn packages"
            )
        self.store, self.policy, self.root = store, policy, root
        self.policy_path = policy_path
        self.agent_id, self.uid = agent_id, uid
        self.expected_operator = digest(expected_operator)
        self.network_id = network_id
        if (
            type(group_id) is not UUID
            or group_id.int == 0
            or type(group_epoch) is not int
            or not 1 <= group_epoch < 2**31
        ):
            raise ScopeDeniedError(
                "Agent deployment requires an explicit group UUID and current epoch"
            )
        self.group_id, self.group_epoch = group_id, group_epoch
        self.group_rollover = (
            "syn.agent-group-rollover/1" in selected["required_profiles"]
            and "group-member-revocation" in selected["required_capabilities"]
        )
        if device_profile != "syn.host-bound-virtual-device/1":
            raise ScopeDeniedError("Select an explicit owner-approved Syn device profile")
        self.device_profile = device_profile

    def _accepts_registration_epoch(self, initial_epoch: object) -> bool:
        # Syn keeps the enrollment config immutable and owns the current epoch.
        # Live status must still prove the exact epoch selected by this controller.
        return type(initial_epoch) is int and (
            initial_epoch == self.group_epoch
            or (self.group_rollover and 1 <= initial_epoch < self.group_epoch)
        )

    def _installation(self) -> tuple[dict[str, Any], Path]:
        if (
            self.policy_path is not None
            and Policy.parse(private_read(self.policy_path)).fingerprint != self.policy.fingerprint
        ):
            raise StateMismatchError("Agent release trust policy changed")
        receipt = self.store.verify(self.policy)
        python = self.store.root / "installs" / receipt["directory"] / "venv/bin/python"
        self.store._tool(python)
        return receipt, python

    @contextmanager
    def session(self, *, new_identity: bool = False) -> Iterator[AgentSession]:
        # Verify immutable bytes before creating any restricted OS resources.
        receipt, python = self._installation()
        if not new_identity and not (self.root / self.agent_id.hex).exists():
            raise StateMismatchError(
                "Agent host registration is absent; explicit first initialization is required"
            )
        view = runtime_view(self.store, receipt, self.root / ".runtimes")
        host = provision(self.root, agent_id=self.agent_id, uid=self.uid)
        directory = Path(host["home"]) / "syn"
        if new_identity and directory.exists():
            raise StateMismatchError("Agent private state already exists; no identity replacement")
        if not new_identity and not directory.exists():
            raise StateMismatchError(
                "Agent state is absent; explicit first initialization is required"
            )
        metadata = self._public_command(
            python, ["-c", "import json,sys;print(json.dumps({'prefix':sys.base_prefix}))"]
        )
        prefix = Path(metadata["prefix"]).resolve(strict=True)
        roots = tuple(dict.fromkeys((view, prefix)))
        arguments: tuple[str, ...] = (
            "--state-dir",
            str(directory),
            "--keyring-service",
            "syn.agent",
            "--keyring-account",
            "primary",
            "--expected-operator",
            self.expected_operator,
            "--network-id",
            self.network_id,
            "--group-id",
            str(self.group_id),
            "--group-epoch",
            str(self.group_epoch),
            "--approved-device-profile",
            self.device_profile,
        )
        if new_identity:
            arguments += ("--new-identity",)
        with prepared_launch(
            host=host, python=view / "bin/python", runtime_roots=roots, arguments=arguments
        ) as launch:
            process = subprocess.Popen(
                launch.argv,
                pass_fds=launch.pass_fds,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self._environment(),
            )
            session = AgentSession(process, group_rollover=self.group_rollover)
            try:
                initial = session.read()
                session.initial = initial
                scope = initial["scope"]
                if (
                    scope["uid"] != self.uid
                    or scope["gid"] != self.uid
                    or not all(
                        scope[name] is True
                        for name in (
                            "capabilities_zero",
                            "no_new_privileges",
                            "internet_socket_creation_denied",
                            "root_read_only",
                        )
                    )
                ):
                    raise StateMismatchError(
                        "Syn Agent session did not establish its restricted native profile"
                    )
                # Keep only public source/configuration pins in the host registry.
                registration = {
                    "profile": "potbot.syn-agent-registration/1",
                    "agent_id": str(self.agent_id),
                    "manifest_sha256": self.policy.manifest_sha256,
                    "installation": receipt["directory"],
                    "owner_uid": self.uid,
                    "expected_operator": self.expected_operator,
                    "network_id": self.network_id,
                    "group_id": str(self.group_id),
                    "group_epoch": self.group_epoch,
                    "device_profile": self.device_profile,
                    "configuration_sha256": digest(initial["configuration_sha256"]),
                    "state_dir": str(directory),
                }
                path = directory.parent.parent / "deployment.json"
                if new_identity:
                    _write_new(path, canonical(registration))
                else:
                    retained = document(private_read(path), canonical_only=True)
                    initial_epoch = retained.get("group_epoch")
                    if not self._accepts_registration_epoch(initial_epoch) or retained != {
                        **registration,
                        "group_epoch": initial_epoch,
                    }:
                        raise StateMismatchError(
                            "Agent deployment differs from independently retained registration"
                        )
                yield session
            finally:
                session.stop()

    def retain_assignment(
        self, status: dict[str, Any], *, adoption_receipt: dict[str, Any], session: AgentSession
    ) -> dict[str, Any]:
        """Pin an observed selected identity, then verify it with a fresh challenge."""
        _, python = self._installation()
        directory = self.root / self.agent_id.hex
        registration = document(private_read(directory / "deployment.json"), canonical_only=True)
        body = status["status"]
        selected_hash = hashlib.sha256(bytes.fromhex(body["selected_public_key"])).hexdigest()
        public_input = directory / ("verify-" + uuid4().hex + ".json")
        _write_new(public_input, canonical(adoption_receipt))
        try:
            certificate = self._public_command(
                python,
                [
                    "-m",
                    "syn_client.adoption",
                    "--receipt",
                    str(public_input),
                    "--expected-operator",
                    self.expected_operator,
                    "--historical-assignment",
                ],
            )
        finally:
            public_input.unlink()
        if certificate["selected_public_key_hash"] != selected_hash:
            raise StateMismatchError(
                "Live Agent key differs from independently verified operator assignment"
            )
        record = {
            "profile": "potbot.syn-agent-assignment/1",
            "selected_public_key_hash": selected_hash,
            "assignment_sha256": digest(body["assignment_sha256"]),
            "registration": registration,
            "certificate_sha256": certificate["receipt_sha256"],
        }
        path = directory / "assignment.json"
        verified = self._status(python, record)
        session.observe(verified, owner_uid=self.uid)
        if path.exists():
            if document(private_read(path), canonical_only=True) != record:
                raise StateMismatchError("Agent selected identity differs from retained assignment")
        else:
            # Public response only; canonical client independently proves the
            # selected key on the actual private native endpoint before commit.
            _write_new(path, canonical(record))
        return verified

    def _status(self, python: Path, record: dict[str, Any]) -> dict[str, Any]:
        registration = record["registration"]
        if (
            registration["agent_id"] != str(self.agent_id)
            or registration["owner_uid"] != self.uid
            or registration["expected_operator"] != self.expected_operator
            or registration["manifest_sha256"] != self.policy.manifest_sha256
            or registration["network_id"] != self.network_id
            or registration["group_id"] != str(self.group_id)
            or not self._accepts_registration_epoch(registration["group_epoch"])
            or registration["state_dir"] != str(self.root / self.agent_id.hex / "home/syn")
        ):
            raise StateMismatchError(
                "Agent public assignment differs from selected host, release or operator policy"
            )
        value = self._public_command(
            python,
            [
                "-m",
                "syn_client.agent",
                "--socket",
                str(Path(registration["state_dir"]) / "agent.sock"),
                "--owner-uid",
                str(self.uid),
                "--expected-key",
                record["selected_public_key_hash"],
                "--expected-operator",
                self.expected_operator,
                "--expected-version",
                self.policy.value["version"],
                "--configuration-sha256",
                registration["configuration_sha256"],
                "--assignment-sha256",
                record["assignment_sha256"],
                "--expected-group",
                str(self.group_id),
                "--expected-epoch",
                str(self.group_epoch),
            ],
        )
        if (
            value["status"]["cog_execution_authorized"] is not False
            or value["status"]["network_id"] != self.network_id
            or value["status"]["runtime"] != self.store.verify(self.policy)["observed"]["runtime"]
        ):
            raise StateMismatchError("Agent provisioning cannot grant Cog authority")
        return value

    def attach(self) -> dict[str, Any]:
        _, python = self._installation()
        record = document(
            private_read(self.root / self.agent_id.hex / "assignment.json"), canonical_only=True
        )
        return self._status(python, record)
