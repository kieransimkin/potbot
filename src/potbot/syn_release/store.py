"""Linux operator store: verified CAS, serialized installs and atomic admission.

Virtual environments stay at their final path (moving them breaks script paths).
An incomplete directory is never admitted. Only the atomically replaced registry
makes a fully checked, read-only installation visible to the controller.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import selectors
import signal
import stat
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    ScopeDeniedError,
    SourceUnavailableError,
    StateMismatchError,
)
from potbot.syn_release.contracts import (
    DOCUMENT_LIMIT,
    Policy,
    Release,
    canonical,
    digest,
    document,
    inspect_requirements,
    inspect_wheel,
    verify_manifest,
    version,
)
from potbot.syn_release.source import GitHubSource, Publication, publication_record


def _sha(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(65_536):
            result.update(chunk)
    return result.hexdigest()


def _directory(path: Path, *, create: bool = False) -> None:
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise ScopeDeniedError("Syn store requires a canonical absolute operator path")
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ScopeDeniedError("Syn store directory must be private and operator-owned")


def private_read(path: Path, limit: int = DOCUMENT_LIMIT) -> bytes:
    _directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or not 0 < info.st_size <= limit
        ):
            raise ScopeDeniedError(
                "Syn store file has unsafe ownership, links, permissions or size"
            )
        with os.fdopen(os.dup(fd), "rb") as source:
            raw = source.read(limit + 1)
        if len(raw) != info.st_size:
            raise StateMismatchError("Syn store file changed during read")
        return raw
    finally:
        os.close(fd)


def _write_new(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# Executed only by the authenticated installed client, under isolated Python.
_CONTRACT_CHECK = """
import json, sys
from importlib.metadata import version
from pathlib import Path
from syn_client.releases import ReleaseManifest, ReleasePin, RuntimeContract, check_compatibility, verify_release
p = json.loads(sys.argv[1])
raw = Path(sys.argv[2]).read_bytes()
m = ReleaseManifest.from_bytes(raw)
if p['component'] == 'runtime':
    verify_release(raw, Path(sys.argv[2]).with_name('syn-release-' + p['version'] + '.sig.json').read_bytes(),
        ReleasePin(p['version'], p['source_commit'], p['manifest_sha256'], p['release_key_sha256']),
        bytes.fromhex(p['public_key']))
observed = json.loads(Path(sys.argv[3]).read_text()) if p['component'] == 'runtime' else None
r = RuntimeContract.from_dict(observed) if observed is not None else m.runtime
assert version('syn-client') == p['version']
assert p['component'] != 'runtime' or version('syn') == p['version']
check_compatibility(m, r, component=p['component'], platform='linux-x86_64',
    python_version=p['python_version'], required_profiles=tuple(p['required_profiles']),
    required_capabilities=tuple(p['required_capabilities']), required_mode=p['mode'])
print(json.dumps({'client_version': version('syn-client'), 'runtime': observed}))
"""


class SynReleaseStore:
    """Host-controller API, deliberately absent from model tools and web routes.

    Provision the root outside all Agent homes. Package installation does not
    create identities, touch a database, start a node or grant execution rights.
    """

    def __init__(self, root: Path) -> None:
        if (
            os.name != "posix"
            or platform.system() != "Linux"
            or platform.machine().lower() not in {"x86_64", "amd64"}
        ):
            raise CapabilityUnavailableError(
                "Syn release installation currently requires Linux x86-64"
            )
        _directory(root, create=True)
        self.root = root
        for name in ("objects", "bundles", "installs", "inventories", "process-home", "uv-cache"):
            _directory(root / name, create=True)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl

        fd = os.open(
            self.root / "install.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
        try:
            info = os.fstat(fd)
            if (
                info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or not stat.S_ISREG(info.st_mode)
            ):
                raise ScopeDeniedError("Syn install lock is not operator-private")
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise StateMismatchError(
                            "Another Syn installation owns the store; retry after it finishes"
                        ) from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def _cached_object(self, expected: str, size: int) -> Path:
        path = self.root / "objects" / expected
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_size != size
            or _sha(path) != expected
        ):
            raise StateMismatchError("Cached Syn payload is absent, linked or altered")
        return path

    def _bundle(self, policy: Policy, source: GitHubSource | None, offline: bool) -> Release:
        bundle = self.root / "bundles" / policy.manifest_sha256
        v = policy.value["version"]
        manifest_name, signature_name = f"syn-release-{v}.json", f"syn-release-{v}.sig.json"
        if offline:
            if not bundle.exists():
                raise SourceUnavailableError("Selected Syn release is not retained for offline use")
            _directory(bundle)
            release = verify_manifest(
                private_read(bundle / manifest_name), private_read(bundle / signature_name), policy
            )
        else:
            source = source or GitHubSource()
            publication = source.publication(policy)
            by_name = {a.name: a for a in publication.assets}
            envelopes = []
            for name in (manifest_name, signature_name):
                if name not in by_name or by_name[name].size > DOCUMENT_LIMIT:
                    raise InvalidInputError("Syn release envelope is missing or oversized")
                output = io.BytesIO()
                source.download(by_name[name], output)
                if len(output.getvalue()) != by_name[name].size:
                    raise StateMismatchError("Downloaded Syn envelope size differs")
                envelopes.append(output.getvalue())
            release = verify_manifest(envelopes[0], envelopes[1], policy)
            expected = {a.name: a.size for a in release.assets}
            expected.update(
                {manifest_name: len(release.raw), signature_name: len(release.signature)}
            )
            if {a.name: a.size for a in publication.assets} != expected:
                raise StateMismatchError(
                    "Published Syn asset set differs from the signed package set"
                )
            # Reject incompatible releases before retrieving or executing package code.
            release.compatibility("linux-x86_64", policy.value["python_version"])
            for asset in release.assets:
                path = self.root / "objects" / asset.sha256
                if path.exists() or path.is_symlink():
                    self._cached_object(asset.sha256, asset.size)
                    continue
                temporary = self.root / "objects" / (uuid4().hex + ".partial")
                try:
                    with temporary.open("xb") as payload_output:
                        os.chmod(temporary, 0o600)
                        source.download(by_name[asset.name], payload_output)
                        payload_output.flush()
                        os.fsync(payload_output.fileno())
                    if temporary.stat().st_size != asset.size or _sha(temporary) != asset.sha256:
                        raise StateMismatchError(
                            "Downloaded Syn payload differs from its signed hash or size"
                        )
                    temporary.chmod(0o400)
                    os.replace(temporary, path)
                    _sync_directory(path.parent)
                finally:
                    temporary.unlink(missing_ok=True)
            if bundle.exists():
                if (
                    private_read(bundle / manifest_name) != release.raw
                    or private_read(bundle / signature_name) != release.signature
                ):
                    raise StateMismatchError("Retained Syn envelope was altered")
            else:
                self._save_bundle(bundle, manifest_name, signature_name, release, publication)
        release.compatibility("linux-x86_64", policy.value["python_version"])
        for asset in release.assets:
            self._cached_object(asset.sha256, asset.size)
        return release

    def _save_bundle(
        self,
        bundle: Path,
        manifest_name: str,
        signature_name: str,
        release: Release,
        publication: Publication,
    ) -> None:
        temporary = bundle.parent / (uuid4().hex + ".partial")
        temporary.mkdir(mode=0o700)
        _write_new(temporary / manifest_name, release.raw)
        _write_new(temporary / signature_name, release.signature)
        _write_new(temporary / "publication.json", publication_record(publication))
        _sync_directory(temporary)
        os.replace(temporary, bundle)
        _sync_directory(bundle.parent)

    def fetch(
        self, policy: Policy, *, source: GitHubSource | None = None, offline: bool = False
    ) -> Release:
        policy = Policy.parse(canonical(policy.value))
        with self._locked():
            return self._bundle(policy, source, offline)

    def _run(self, argv: list[str], *, label: str, timeout: int = 600) -> bytes:
        # No GitHub token, user Python path, pip/uv config, keyring or model-selected environment.
        env = {
            "HOME": str(self.root / "process-home"),
            "PATH": "/usr/bin:/bin",
            "UV_CACHE_DIR": str(self.root / "uv-cache"),
            "UV_NO_CONFIG": "1",
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_LINK_MODE": "copy",
            "UV_NO_PROGRESS": "1",
        }
        for key in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"):
            if key in os.environ:
                env[key] = os.environ[key]
        process = subprocess.Popen(
            argv,
            cwd=self.root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        assert process.stdout is not None
        data = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + timeout
                while True:
                    if time.monotonic() >= deadline:
                        raise StateMismatchError(f"Syn {label} exceeded its process deadline")
                    if not selector.select(timeout=0.1):
                        continue
                    part = os.read(process.stdout.fileno(), min(65_536, 1_048_577 - len(data)))
                    if not part:
                        break
                    data.extend(part)
                    if len(data) > 1_048_576:
                        raise StateMismatchError(f"Syn {label} exceeded its output bound")
            if process.wait(timeout=5) != 0:
                _write_new(self.root / ("process-" + uuid4().hex + ".log"), bytes(data))
                raise StateMismatchError(f"Syn {label} failed; installation is not admitted")
            return bytes(data)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            process.stdout.close()

    def _tool(self, path: Path) -> tuple[str, str]:
        if not path.is_absolute():
            raise ScopeDeniedError("Installer tools require explicit absolute executable paths")
        path = path.resolve(strict=True)
        info = path.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.getuid()}
            or info.st_mode & 0o022
        ):
            raise ScopeDeniedError("Installer executable is writable by another principal")
        for parent in path.parents:
            parent_info = parent.stat()
            sticky_root = parent_info.st_uid == 0 and parent_info.st_mode & stat.S_ISVTX
            if parent_info.st_uid not in {0, os.getuid()} or (
                parent_info.st_mode & 0o022 and not sticky_root
            ):
                raise ScopeDeniedError("Installer executable has an untrusted writable parent")
        return str(path), _sha(path)

    def _observe(self, candidate: Path, release: Release) -> dict[str, Any]:
        python = str(candidate / "venv/bin/python")
        raw = self._run(
            [
                python,
                "-I",
                "-B",
                "-c",
                "import sys;print(str(sys.version_info.major)+'.'+str(sys.version_info.minor))",
            ],
            label="installed Python probe",
            timeout=30,
        )
        if raw.decode().strip() != release.policy.value["python_version"]:
            raise StateMismatchError("Installed Syn interpreter differs from selected Python")
        descriptor = candidate / "observed-runtime.json"
        if release.policy.value["component"] == "runtime":
            raw = self._run(
                [python, "-I", "-B", "-m", "syn.server", "--describe-json"],
                label="runtime descriptor",
                timeout=30,
            )
            if document(raw) != release.runtime:
                raise StateMismatchError(
                    "Installed Syn descriptor differs from the authenticated release"
                )
            # Verification never needs to rewrite a sealed installation.
            if descriptor.exists():
                if document(descriptor.read_bytes()) != release.runtime:
                    raise StateMismatchError("Retained installed Syn descriptor changed")
            else:
                _write_new(descriptor, canonical(release.runtime))
        assets = candidate / "assets"
        result = self._run(
            [
                python,
                "-I",
                "-B",
                "-c",
                _CONTRACT_CHECK,
                canonical(release.policy.value).decode(),
                str(assets / f"syn-release-{release.policy.value['version']}.json"),
                str(descriptor),
            ],
            label="official client compatibility check",
            timeout=30,
        )
        return document(result)

    def _build(
        self, candidate: Path, release: Release, python: str, uv: str, offline: bool
    ) -> dict[str, Any]:
        selected = release.policy.value
        raw = self._run(
            [
                python,
                "-I",
                "-B",
                "-c",
                "import sys;print(str(sys.version_info.major)+'.'+str(sys.version_info.minor))",
            ],
            label="selected Python probe",
            timeout=30,
        )
        if raw.decode().strip() != selected["python_version"]:
            raise ScopeDeniedError(
                "Operator-selected Python does not match the compatibility policy"
            )
        if self._run([uv, "--version"], label="uv version", timeout=10).decode().split()[:2] != [
            "uv",
            "0.12.23",
        ]:
            raise ScopeDeniedError("Syn installer requires reviewed uv 0.12.23")
        self._run(
            [
                python,
                "-I",
                "-B",
                "-m",
                "venv",
                "--copies",
                "--without-pip",
                str(candidate / "venv"),
            ],
            label="virtual environment creation",
        )
        target = str(candidate / "venv/bin/python")
        common = [uv, "--no-config", "pip", "install", "--python", target, "--link-mode", "copy"]
        if offline:
            common.append("--offline")
        assets = candidate / "assets"
        if selected["component"] == "runtime":
            inspect_requirements(assets / f"syn-runtime-requirements-{selected['version']}.txt")
            self._run(
                [
                    *common,
                    "--require-hashes",
                    "--only-binary",
                    ":all:",
                    "--index-url",
                    "https://pypi.org/simple",
                    "-r",
                    str(assets / f"syn-runtime-requirements-{selected['version']}.txt"),
                ],
                label="hash-locked runtime dependencies",
            )
        wheels = [
            str(assets / a.name)
            for a in release.assets
            if a.role == "client-wheel"
            or (a.role == "runtime-wheel" and selected["component"] == "runtime")
        ]
        for wheel in wheels:
            inspect_wheel(Path(wheel))
        self._run(
            [*common, "--no-cache", "--no-index", "--no-deps", *wheels],
            label="authenticated official wheels",
        )
        return self._observe(candidate, release)

    def _inventory(self, directory: Path) -> dict[str, Any]:
        if directory.is_symlink() or directory.stat().st_mode & 0o222:
            raise StateMismatchError("Installed Syn root is writable or linked")
        result: dict[str, Any] = {}
        for parent, directories, files in os.walk(directory, followlinks=False):
            for name in sorted([*directories, *files]):
                path = Path(parent) / name
                key = path.relative_to(directory).as_posix()
                info = path.lstat()
                if info.st_uid != os.getuid() or (
                    not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o222
                ):
                    raise StateMismatchError(
                        "Installed Syn content is writable or has changed owner"
                    )
                if stat.S_ISLNK(info.st_mode):
                    if not path.resolve().is_relative_to(directory):
                        raise ScopeDeniedError("Installed Syn link escapes its immutable directory")
                    result[key] = {"link": os.readlink(path)}
                elif stat.S_ISREG(info.st_mode):
                    if info.st_nlink != 1:
                        raise ScopeDeniedError(
                            "Installed Syn content must not share mutable hardlinks"
                        )
                    result[key] = {
                        "size": info.st_size,
                        "sha256": _sha(path),
                        "mode": stat.S_IMODE(info.st_mode),
                    }
                elif stat.S_ISDIR(info.st_mode):
                    result[key] = {"directory": True, "mode": stat.S_IMODE(info.st_mode)}
                else:
                    raise ScopeDeniedError("Unexpected special file in installed Syn package")
                if len(result) > 50_000:
                    raise InvalidInputError("Installed Syn file inventory exceeds its bound")
        return result

    def _dependency_inventory(self) -> bytes:
        cache = self.root / "uv-cache"
        result: dict[str, Any] = {}
        for parent, directories, files in os.walk(cache, followlinks=False):
            for name in sorted([*directories, *files]):
                path = Path(parent) / name
                info = path.lstat()
                if info.st_uid != os.getuid():
                    raise ScopeDeniedError("Dependency cache has a different owner")
                key = path.relative_to(cache).as_posix()
                if stat.S_ISLNK(info.st_mode):
                    if not path.resolve().is_relative_to(cache):
                        raise ScopeDeniedError("Dependency cache link escapes the retained cache")
                    result[key] = {"link": os.readlink(path)}
                elif stat.S_ISREG(info.st_mode):
                    if info.st_nlink != 1:
                        raise ScopeDeniedError("Dependency cache must not share mutable hardlinks")
                    result[key] = {"sha256": _sha(path), "size": info.st_size}
                elif stat.S_ISDIR(info.st_mode):
                    result[key] = {"directory": True}
                else:
                    raise ScopeDeniedError("Unexpected dependency cache object")
                if len(result) > 50_000:
                    raise InvalidInputError("Dependency cache inventory exceeds its bound")
        raw = canonical(result)
        if len(raw) > 16_777_216:
            raise InvalidInputError("Dependency cache inventory exceeds its byte bound")
        return raw

    def _check_dependency_cache(
        self, policy: Policy, history: list[dict[str, Any]], offline: bool
    ) -> None:
        if policy.value["component"] != "runtime":
            return  # Official wheels use --no-cache and cannot consume cached unpacked code.
        seal = self.root / "dependency-cache-inventory.json"
        expected = policy.value["dependency_cache_sha256"]
        if expected is None and history:
            expected = history[-1]["dependency_cache_sha256"]
        if offline and policy.value["dependency_cache_sha256"] is None:
            raise ScopeDeniedError(
                "Fresh offline runtime installation requires an independently retained dependency-cache digest"
            )
        observed = self._dependency_inventory()
        if seal.exists() or seal.is_symlink():
            raw = private_read(seal, 16_777_216)
            if expected is None or hashlib.sha256(raw).hexdigest() != expected or raw != observed:
                raise StateMismatchError(
                    "Retained dependency cache differs from its independently admitted inventory"
                )
        elif offline or observed != b"{}" or expected is not None:
            raise StateMismatchError(
                "Dependency cache lacks its admitted inventory; reconcile or prepare a fresh store"
            )

    def _seal_dependency_cache(self) -> str:
        raw = self._dependency_inventory()
        temporary = self.root / (uuid4().hex + ".cache-inventory")
        _write_new(temporary, raw)
        os.replace(temporary, self.root / "dependency-cache-inventory.json")
        _sync_directory(self.root)
        return hashlib.sha256(raw).hexdigest()

    def _registry(self) -> list[dict[str, Any]]:
        path = self.root / "registry.json"
        if path.is_symlink():
            raise ScopeDeniedError("Syn journal cannot be a symbolic link")
        if not path.exists():
            return []
        raw = private_read(path)
        value = document(raw)
        if (
            set(value) != {"profile", "installations"}
            or value["profile"] != "potbot.syn-install-journal/1"
            or type(value["installations"]) is not list
            or len(value["installations"]) > 32
        ):
            raise StateMismatchError("Syn installation journal is invalid; do not recreate it")
        expected = {
            "directory",
            "component",
            "mode",
            "version",
            "source_commit",
            "manifest_sha256",
            "release_key_sha256",
            "policy_sha256",
            "files_sha256",
            "artifacts",
            "python_sha256",
            "uv_sha256",
            "observed",
            "installed_at",
            "node_qualified",
            "acquisition",
            "dependency_cache_sha256",
        }
        seen: set[str] = set()
        for record in value["installations"]:
            if type(record) is not dict or set(record) != expected:
                raise StateMismatchError("Invalid Syn journal entry")
            version(record["version"])
            for name in (
                "manifest_sha256",
                "release_key_sha256",
                "policy_sha256",
                "files_sha256",
                "python_sha256",
                "uv_sha256",
            ):
                digest(record[name])
            if (
                record["component"] not in {"runtime", "client"}
                or record["node_qualified"] is not False
            ):
                raise StateMismatchError("Invalid Syn admission component or qualification")
            name = record["component"] + "-" + record["version"] + "-" + record["manifest_sha256"]
            if record["directory"] != name or name in seen:
                raise StateMismatchError("Ambiguous or escaping Syn admission directory")
            seen.add(name)
        return list(value["installations"])

    def _verify(self, candidate: Path, record: dict[str, Any], release: Release) -> dict[str, Any]:
        selected = release.policy.value
        if (
            record["version"] != selected["version"]
            or record["source_commit"] != selected["source_commit"]
            or record["manifest_sha256"] != selected["manifest_sha256"]
            or record["release_key_sha256"] != selected["release_key_sha256"]
        ):
            raise StateMismatchError("Syn admission differs from the selected release identity")
        _directory(candidate)
        inventory_path = self.root / "inventories" / digest(record["files_sha256"])
        raw = private_read(inventory_path, 16_777_216)
        if (
            hashlib.sha256(raw).hexdigest() != record["files_sha256"]
            or canonical(self._inventory(candidate)) != raw
        ):
            raise StateMismatchError("Installed Syn files differ from the admitted inventory")
        if record["artifacts"] != [
            {"role": a.role, "sha256": a.sha256, "size": a.size} for a in release.assets
        ]:
            raise StateMismatchError(
                "Retained Syn artifact provenance differs from the signed manifest"
            )
        original_policy = private_read(candidate / "policy.json")
        if hashlib.sha256(original_policy).hexdigest() != record["policy_sha256"]:
            raise StateMismatchError(
                "Retained Syn policy provenance differs from its admitted digest"
            )
        if self._observe(candidate, release) != record["observed"]:
            raise StateMismatchError(
                "Installed Syn compatibility differs from the admitted observation"
            )
        return {
            **record,
            "checked_policy_sha256": release.policy.fingerprint,
            "qualification": "verified_package_installation_only",
            "node_qualified": False,
        }

    def install(
        self,
        policy: Policy,
        *,
        python: Path,
        uv: Path,
        source: GitHubSource | None = None,
        offline: bool = False,
    ) -> dict[str, Any]:
        policy = Policy.parse(canonical(policy.value))
        with self._locked():
            release = self._bundle(policy, source, offline)
            history = self._registry()
            selected = policy.value
            same_component = [r for r in history if r["component"] == selected["component"]]
            if any(version(r["version"]) > version(selected["version"]) for r in same_component):
                raise ScopeDeniedError("Syn install would downgrade retained admission history")
            if any(
                r["version"] == selected["version"]
                and r["manifest_sha256"] != policy.manifest_sha256
                for r in same_component
            ):
                raise StateMismatchError("An admitted Syn version cannot be redefined")
            name = selected["component"] + "-" + selected["version"] + "-" + policy.manifest_sha256
            candidate = self.root / "installs" / name
            known = [r for r in same_component if r["directory"] == name]
            if known:
                return self._verify(candidate, known[0], release)
            if candidate.exists() or candidate.is_symlink():
                raise StateMismatchError(
                    "Incomplete Syn installation requires operator reconciliation; never execute it"
                )
            if len(history) >= 32:
                raise StateMismatchError(
                    "Syn admission history is full; retain and reconcile it explicitly"
                )
            self._check_dependency_cache(policy, history, offline)
            python_path, python_sha = self._tool(python)
            uv_path, uv_sha = self._tool(uv)
            candidate.mkdir(mode=0o700)
            assets = candidate / "assets"
            assets.mkdir(mode=0o700)
            for asset in release.assets:
                source_path = self._cached_object(asset.sha256, asset.size)
                destination = assets / asset.name
                with source_path.open("rb") as source_file, destination.open("xb") as output_file:
                    os.chmod(destination, 0o600)
                    while chunk := source_file.read(65_536):
                        output_file.write(chunk)
                    output_file.flush()
                    os.fsync(output_file.fileno())
                if _sha(destination) != asset.sha256 or destination.stat().st_size != asset.size:
                    raise StateMismatchError("Staged Syn payload changed before installation")
            _write_new(assets / f"syn-release-{selected['version']}.json", release.raw)
            _write_new(assets / f"syn-release-{selected['version']}.sig.json", release.signature)
            _write_new(candidate / "policy.json", canonical(selected))
            observed = self._build(candidate, release, python_path, uv_path, offline)
            for parent, directories, files in os.walk(candidate, topdown=False, followlinks=False):
                for item in [*files, *directories]:
                    path = Path(parent) / item
                    if not path.is_symlink():
                        path.chmod(0o500 if path.is_dir() or path.stat().st_mode & 0o111 else 0o400)
            candidate.chmod(0o500)
            dependency_cache_sha = self._seal_dependency_cache()
            inventory = canonical(self._inventory(candidate))
            inventory_sha = hashlib.sha256(inventory).hexdigest()
            _write_new(self.root / "inventories" / inventory_sha, inventory)
            record = {
                "directory": name,
                "component": selected["component"],
                "mode": selected["mode"],
                "version": selected["version"],
                "source_commit": selected["source_commit"],
                "manifest_sha256": policy.manifest_sha256,
                "release_key_sha256": selected["release_key_sha256"],
                "policy_sha256": policy.fingerprint,
                "files_sha256": inventory_sha,
                "artifacts": [
                    {"role": a.role, "sha256": a.sha256, "size": a.size} for a in release.assets
                ],
                "python_sha256": python_sha,
                "uv_sha256": uv_sha,
                "dependency_cache_sha256": dependency_cache_sha,
                "observed": observed,
                "installed_at": datetime.now(UTC).isoformat(),
                "node_qualified": False,
                "acquisition": "offline_retained_signature_verified"
                if offline
                else "online_immutable_verified",
            }
            next_journal = canonical(
                {"profile": "potbot.syn-install-journal/1", "installations": [*history, record]}
            )
            if len(next_journal) > DOCUMENT_LIMIT:
                raise StateMismatchError("Syn admission journal exceeds its bound")
            temporary = self.root / (uuid4().hex + ".journal")
            _write_new(temporary, next_journal)
            # Single durable atomic publication of provenance and append-only history.
            _sync_directory(candidate)
            os.replace(temporary, self.root / "registry.json")
            _sync_directory(self.root)
            return {**record, "qualification": "verified_package_installation_only"}

    def verify(self, policy: Policy) -> dict[str, Any]:
        policy = Policy.parse(canonical(policy.value))
        with self._locked():
            release = self._bundle(policy, None, True)
            matches = [
                r
                for r in self._registry()
                if r["manifest_sha256"] == policy.manifest_sha256
                and r["component"] == policy.value["component"]
            ]
            if len(matches) != 1:
                raise StateMismatchError(
                    "No unique admitted installation for the selected Syn release"
                )
            record = matches[0]
            expected = (
                policy.value["component"]
                + "-"
                + policy.value["version"]
                + "-"
                + policy.manifest_sha256
            )
            if record["directory"] != expected:
                raise StateMismatchError(
                    "Syn installation directory differs from its independent pin"
                )
            return self._verify(self.root / "installs" / expected, record, release)
