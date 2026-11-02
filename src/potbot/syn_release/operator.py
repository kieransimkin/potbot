"""Deploy and supervise a verified upstream operator; no identity implementation.

Every key/state operation is an explicit official Syn CLI operation. This module
stores public installation/identity pins and owns only the process it launches.
"""

from __future__ import annotations

import hashlib
import os
import selectors
import signal
import stat
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.syn_release.commands import SynPublicCommands
from potbot.syn_release.contracts import Policy, canonical, digest, document, fields
from potbot.syn_release.store import (
    SynReleaseStore,
    _directory,
    _sync_directory,
    _write_new,
    private_read,
)


class SynOperatorDeployment(SynPublicCommands):
    def __init__(
        self, store: SynReleaseStore, policy: Policy, root: Path, *, policy_path: Path | None = None
    ):
        selected = policy.value
        if (
            selected["component"] != "runtime"
            or selected["mode"] != "operator"
            or "syn.operator-local/1" not in selected["required_profiles"]
            or not {
                "authenticated-operator-ipc",
                "native-os-operator-custody",
                "retained-operator-identity",
            }
            <= set(selected["required_capabilities"])
        ):
            raise ScopeDeniedError(
                "Operator deployment requires explicit upstream operator contracts"
            )
        _directory(root, create=True)
        if root.is_relative_to(store.root) or store.root.is_relative_to(root):
            raise ScopeDeniedError(
                "Operator mutable state must be separate from immutable package storage"
            )
        self.store, self.policy, self.root = store, policy, root
        self.policy_path = policy_path
        self.registration = root / "operator-registration.json"
        self.state_dir = root / "syn-state"

    def _installation(self) -> tuple[dict[str, Any], Path]:
        receipt = self.store.verify(self.policy)
        python = self.store.root / "installs" / receipt["directory"] / "venv/bin/python"
        self.store._tool(python)
        return receipt, python

    def unlock(self) -> None:
        _, python = self._installation()
        # Explicit interactive handoff to verified Syn. Potbot never reads the
        # controlling terminal, password, keyring value or private key.
        result = subprocess.run(
            [str(python), "-I", "-B", "-m", "syn.operator", "unlock"],
            cwd=self.root,
            env=self._environment(),
            check=False,
        )
        if result.returncode:
            raise StateMismatchError("Native keystore unlock requires its private local Syn prompt")


    def bootstrap(
        self,
        *,
        keyring_service: str,
        keyring_account: str,
        network_id: str,
        new_identity: bool = False,
    ) -> dict[str, Any]:
        if not new_identity:
            raise ScopeDeniedError(
                "First operator creation requires explicit new-identity authorization"
            )
        receipt, python = self._installation()
        # Reuse the existing bounded private store lock to serialize host setup.
        with self.store._locked():
            if (
                self.registration.exists()
                or self.registration.is_symlink()
                or self.state_dir.exists()
            ):
                raise StateMismatchError(
                    "Operator registration/state exists; creation cannot repair it"
                )
            _directory(self.state_dir, create=True)
            value = self._public_command(
                python,
                [
                    "-m",
                    "syn.operator",
                    "init",
                    "--state-dir",
                    str(self.state_dir),
                    "--keyring-service",
                    keyring_service,
                    "--keyring-account",
                    keyring_account,
                    "--network-id",
                    network_id,
                    "--new-identity",
                ],
                timeout=60,
            )
            # Persist only the documented public receipt fields.
            expected_hash = digest(value["identity"]["base_public_key_hash"])
            configuration_sha = digest(value["configuration_sha256"])
            record = {
                "profile": "potbot.syn-operator-registration/1",
                "manifest_sha256": self.policy.manifest_sha256,
                "installation": receipt["directory"],
                "base_public_key_hash": expected_hash,
                "configuration_sha256": configuration_sha,
                "owner_uid": os.getuid(),
                "state_dir": str(self.state_dir),
                "custody": value["custody"],
            }
            _write_new(self.registration, canonical(record))
            return {**record, "running": False, "agent_activation_authorized": False}

    def _registered(self) -> dict[str, Any]:
        record = fields(
            document(private_read(self.registration), canonical_only=True),
            {
                "profile",
                "manifest_sha256",
                "installation",
                "base_public_key_hash",
                "configuration_sha256",
                "owner_uid",
                "state_dir",
                "custody",
            },
        )
        if (
            record["profile"] != "potbot.syn-operator-registration/1"
            or record["manifest_sha256"] != self.policy.manifest_sha256
            or record["owner_uid"] != os.getuid()
            or record["state_dir"] != str(self.state_dir)
        ):
            raise StateMismatchError(
                "Operator registration differs from its independent host/release pin"
            )
        digest(record["base_public_key_hash"])
        digest(record["configuration_sha256"])
        configuration = private_read(self.state_dir / "operator.json")
        if hashlib.sha256(configuration).hexdigest() != record["configuration_sha256"]:
            raise StateMismatchError("Operator configuration changed after registration")
        return record

    def _status(self, python: Path, record: dict[str, Any]) -> dict[str, Any]:
        if (
            self.policy_path is not None
            and Policy.parse(private_read(self.policy_path)).fingerprint != self.policy.fingerprint
        ):
            raise StateMismatchError(
                "Operator trust policy changed; explicit reconciliation is required"
            )
        value = self._public_command(
            python,
            [
                "-m",
                "syn_client.operator",
                "--socket",
                str(self.state_dir / "operator.sock"),
                "--expected-key",
                record["base_public_key_hash"],
                "--expected-version",
                self.policy.value["version"],
                "--configuration-sha256",
                record["configuration_sha256"],
                "--owner-uid",
                str(record["owner_uid"]),
            ],
            timeout=10,
        )
        body = value["status"]
        if body["runtime"] != self._observed_runtime or body["custody"] != record["custody"]:
            raise StateMismatchError("Live operator differs from the selected installed contract")
        return value

    def attach(self) -> dict[str, Any]:
        receipt, python = self._installation()
        record = self._registered()
        if record["installation"] != receipt["directory"]:
            raise StateMismatchError("Operator registration selects another installed package")
        self._observed_runtime = receipt["observed"]["runtime"]
        value = self._status(python, record)
        return {
            "profile": "potbot.syn-operator-attachment/1",
            "manifest_sha256": self.policy.manifest_sha256,
            "base_public_key_hash": record["base_public_key_hash"],
            "attestation": value,
            "process_owned": False,
            "operator_ready": True,
            "qualification": "release_pinned_native_operator_only",
            "agent_activation_authorized": False,
        }

    def _lifecycle(self, value: dict[str, Any]) -> None:
        path = self.root / (uuid4().hex + ".lifecycle")
        _write_new(path, canonical(value))
        os.replace(path, self.root / "lifecycle.json")
        _sync_directory(self.root)

    @contextmanager
    def _supervisor_lock(self) -> Iterator[None]:
        import fcntl

        fd = os.open(
            self.root / "supervisor.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise ScopeDeniedError("Operator supervisor lock is not privately owned")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StateMismatchError(
                    "Another supervisor owns this operator deployment"
                ) from None
            yield
        finally:
            os.close(fd)

    def supervise(self, *, stop: Callable[[], bool] | None = None) -> None:
        with self._supervisor_lock():
            self._supervise(stop=stop)

    def _supervise(self, *, stop: Callable[[], bool] | None = None) -> None:
        receipt, python = self._installation()
        record = self._registered()
        if record["installation"] != receipt["directory"]:
            raise StateMismatchError("Operator package differs from its registration")
        self._observed_runtime = receipt["observed"]["runtime"]
        # The upstream process owns the lifetime authority lock. No stale PID
        # file or failed attach can authorize stopping an unrelated process.
        process = subprocess.Popen(
            [
                str(python),
                "-I",
                "-B",
                "-m",
                "syn.operator",
                "serve",
                "--config",
                str(self.state_dir / "operator.json"),
            ],
            cwd=self.root,
            env=self._environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        assert process.stdout is not None
        evidence: dict[str, Any] = {
            "profile": "potbot.syn-operator-lifecycle/1",
            "pid": process.pid,
            "base_public_key_hash": record["base_public_key_hash"],
            "manifest_sha256": self.policy.manifest_sha256,
            "state": "starting",
            "agent_activation_authorized": False,
        }
        self._lifecycle(evidence)
        output_size = 0
        failed = True
        requested_stop = False
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + 20
                while process.poll() is None:
                    if stop is not None and stop():
                        requested_stop = True
                        break
                    if selector.select(0.1):
                        chunk = os.read(process.stdout.fileno(), 65536)
                        output_size += len(chunk)
                        if output_size > 65536:
                            raise StateMismatchError(
                                "Operator process exceeded its bounded diagnostic output"
                            )
                    try:
                        status = self._status(python, record)
                    except (StateMismatchError, OSError, ValueError):
                        if time.monotonic() >= deadline:
                            raise StateMismatchError(
                                "Operator failed authenticated readiness; restart fenced"
                            ) from None
                        time.sleep(0.2)
                        continue
                    if status["status"]["pid"] != process.pid:
                        raise StateMismatchError(
                            "Authenticated endpoint is not the child this supervisor owns"
                        )
                    evidence = {**evidence, "state": "ready", "attestation": status}
                    self._lifecycle(evidence)
                    # A healthy challenge is lightweight. Rehashing the entire
                    # immutable package on every heartbeat would cause heavy I/O.
                    deadline = time.monotonic() + 8
                    time.sleep(2)
                if not requested_stop:
                    raise StateMismatchError(
                        "Operator exited unexpectedly; retained identity is fenced"
                    )
                failed = False
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            process.stdout.close()
            self._lifecycle(
                {
                    **evidence,
                    "state": "fenced" if failed else "stopped",
                    "exit_code": process.returncode,
                }
            )
