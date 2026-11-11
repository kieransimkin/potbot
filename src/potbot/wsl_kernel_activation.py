"""Stage a verified WSL kernel while retaining the exact host configuration."""

from __future__ import annotations

import base64
import codecs
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path, PureWindowsPath
from typing import Any

from potbot.domain.errors import DomainError, InvalidInputError, StateMismatchError
from potbot.syn_release.host import DISTRO, WslOperatorHost, _registered_distro, run_host
from potbot.tools.temporary import TemporaryDirectory, allocate_file, create_directory
from potbot.wsl_kernel import (
    ARTIFACT_LIMITS,
    MANIFEST,
    SIGNATURE,
    _file_identity,
    _hex,
    _reparse,
    open_bundle_file,
    parse_manifest,
    read_bundle_file,
    verify_bundle,
)

_CONFIG_LIMIT = 64 * 1024
_JOURNAL_LIMIT = 2 * _CONFIG_LIMIT
_KERNEL_RELEASE = re.compile(r"[0-9][A-Za-z0-9_.+-]{0,63}")
_BOOT_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_SECTION = re.compile(r"^[ \t]*\[([^\]\r\n]+)\][ \t]*(?:[;#][^\r\n]*)?(?:\r?\n|$)", re.MULTILINE)
_KERNEL_KEY = re.compile(r"^[ \t]*kernel(?:modules)?[ \t]*=", re.IGNORECASE | re.MULTILINE)


def _config_path(path: PureWindowsPath) -> str:
    if not path.is_absolute() or not re.fullmatch(r"[A-Za-z]:", path.drive):
        raise InvalidInputError("Kernel paths must be absolute Windows drive paths")
    value = str(path)
    if any(ord(character) < 32 or character in '#;"' for character in value):
        raise InvalidInputError("Kernel path contains an unsupported WSL configuration character")
    return value.replace("\\", "\\\\")


def _configured(previous: bytes | None, bundle: PureWindowsPath) -> bytes:
    raw = previous or b""
    if len(raw) > _CONFIG_LIMIT:
        raise InvalidInputError("WSL configuration exceeds its byte bound")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise InvalidInputError("WSL configuration requires UTF-8") from error
    if "\x00" in text or re.search(r"\r(?!\n)", text):
        raise InvalidInputError("WSL configuration uses unsupported line separators")
    sections = list(_SECTION.finditer(text))
    if len(sections) != len(re.findall(r"^[ \t]*\[", text, re.MULTILINE)):
        raise StateMismatchError("WSL configuration has an ambiguous section header")
    wsl = [section for section in sections if section[1].strip().casefold() == "wsl2"]
    if len(wsl) > 1:
        raise StateMismatchError("WSL configuration has more than one wsl2 section")
    newline = "\r\n" if not text or "\r\n" in text else "\n"
    assignments = (
        f"kernel={_config_path(bundle / 'bzImage')}{newline}"
        f"kernelModules={_config_path(bundle / 'modules.vhdx')}{newline}"
    )
    if wsl:
        section = wsl[0]
        end = next((item.start() for item in sections if item.start() > section.start()), len(text))
        if _KERNEL_KEY.search(text, section.end(), end):
            raise StateMismatchError("WSL configuration already selects an unmanaged kernel")
        insertion = ("" if section[0].endswith("\n") else newline) + assignments
        updated = text[: section.end()] + insertion + text[section.end() :]
    else:
        boundary = "" if not text or text.endswith("\n") else newline
        updated = text + boundary + "[wsl2]" + newline + assignments
    result = (codecs.BOM_UTF8 if raw.startswith(codecs.BOM_UTF8) else b"") + updated.encode("utf-8")
    if len(result) > _CONFIG_LIMIT:
        raise InvalidInputError("Staged WSL configuration exceeds its byte bound")
    return result


def _read_local(path: Path, limit: int = _CONFIG_LIMIT) -> bytes | None:
    if path.resolve() != path:
        raise StateMismatchError("WSL activation requires local files without redirected paths")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or _reparse(info) or info.st_nlink != 1:
        raise StateMismatchError("WSL activation requires a regular, unlinked local file")
    if info.st_size:
        return read_bundle_file(path, limit)
    if _file_identity(info) != _file_identity(path.lstat()):
        raise StateMismatchError("WSL activation input changed while being read")
    return b""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class _Change:
    registration: str
    original: bytes | None
    staged_sha256: str
    manifest_sha256: str
    baseline_kernel: str
    baseline_boot: str

    def encode(self) -> bytes:
        return json.dumps(
            {
                "profile": "potbot.wsl-kernel-transaction/1",
                "registration": self.registration,
                "original": None
                if self.original is None
                else base64.b64encode(self.original).decode("ascii"),
                "staged_sha256": self.staged_sha256,
                "manifest_sha256": self.manifest_sha256,
                "baseline_kernel": self.baseline_kernel,
                "baseline_boot": self.baseline_boot,
            },
            sort_keys=True,
        ).encode("ascii")

    @classmethod
    def load(cls, path: Path) -> _Change:
        raw = _read_local(path, _JOURNAL_LIMIT)
        if raw is None:
            raise StateMismatchError("No Potbot WSL kernel change is pending or active")
        try:
            record = json.loads(raw)
            if record["profile"] != "potbot.wsl-kernel-transaction/1":
                raise ValueError
            original = (
                None
                if record["original"] is None
                else base64.b64decode(record["original"], validate=True)
            )
            change = cls(
                record["registration"],
                original,
                record["staged_sha256"],
                record["manifest_sha256"],
                record["baseline_kernel"],
                record["baseline_boot"],
            )
            if (
                not _hex(change.registration, 32)
                or (original is not None and len(original) > _CONFIG_LIMIT)
                or not _hex(change.staged_sha256, 64)
                or not _hex(change.manifest_sha256, 64)
                or not _KERNEL_RELEASE.fullmatch(change.baseline_kernel)
                or not _BOOT_ID.fullmatch(change.baseline_boot)
                or raw != change.encode()
            ):
                raise ValueError
        except (KeyError, ValueError, TypeError, RecursionError) as error:
            raise StateMismatchError("Potbot WSL kernel transaction receipt is invalid") from error
        return change


def _replace_local(
    path: Path, *, before: bytes | None, after: bytes | None, limit: int = _CONFIG_LIMIT
) -> None:
    if _read_local(path, limit) != before:
        raise StateMismatchError("WSL configuration or activation receipt changed independently")
    if before == after:
        return
    if after is None:
        path.unlink()
        return

    fd, temporary = allocate_file(path.parent, prefix=".potbot-kernel-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(after)
            stream.flush()
            os.fsync(stream.fileno())
        if _read_local(path, limit) != before:
            raise StateMismatchError(
                "WSL configuration or activation receipt changed independently"
            )
        if before is None:
            # Windows rename refuses an existing target. Keep the same no-overwrite
            # behavior in the portable transaction tests.
            if os.name == "nt":
                temporary.rename(path)
            else:
                os.link(temporary, path)
        elif os.name == "nt":
            ctypes = import_module("ctypes")
            replace = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
            replace.argtypes = [ctypes.c_wchar_p] * 3 + [
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            replace.restype = ctypes.c_int
            backup = temporary.with_suffix(".bak")
            if not replace(str(path), str(temporary), str(backup), 0, None, None):
                error = ctypes.WinError(ctypes.get_last_error())
                if not path.exists() and backup.is_file():
                    backup.rename(path)
                raise error
            if _read_local(backup, limit) != before:
                if _read_local(path, limit) == after:
                    if not replace(str(path), str(backup), None, 0, None, None):
                        raise ctypes.WinError(ctypes.get_last_error())
                raise StateMismatchError("WSL configuration changed during replacement")
            backup.unlink()
        else:
            temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    if _read_local(path, limit) != after:
        raise StateMismatchError("WSL configuration or activation receipt failed readback")


_BOOT_PROBE = r"""
import gzip
import hashlib
import json
import os
import re
import sys
from pathlib import Path

kernel = os.uname()
result = {
    "kernel_release": kernel.release,
    "architecture": kernel.machine,
    "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
}
if sys.argv[1] == "candidate":
    with gzip.open("/proc/config.gz", "rb") as stream:
        config = stream.read(2 * 1024**2 + 1)
    if not 0 < len(config) <= 2 * 1024**2:
        raise SystemExit("Kernel config exceeds its bound")
    modules = Path("/lib/modules") / kernel.release
    index = modules / "modules.dep"
    if not index.is_file() or not index.stat().st_size:
        raise SystemExit("Kernel modules index is unavailable")
    sample = next((path for path in (modules / "kernel").rglob("*.ko") if path.is_file()), None)
    if sample is None:
        raise SystemExit("Kernel module sample is unavailable")
    with sample.open("rb") as stream:
        data = stream.read(64 * 1024**2 + 1)
    if not 0 < len(data) <= 64 * 1024**2:
        raise SystemExit("Kernel module sample exceeds its bound")
    version = re.search(rb"\0vermagic=([^ \t\r\n\0]+)", data)
    result["module_release"] = version[1].decode("ascii") if version else ""
    result["config_sha256"] = hashlib.sha256(config).hexdigest()
print(json.dumps(result, sort_keys=True))
"""


def _stage_bundle(
    source: Path, cache: Path, *, manifest_sha256: str, public_key: str
) -> dict[str, Any]:
    verify_bundle(source, manifest_sha256=manifest_sha256, public_key=public_key)
    staged = cache / manifest_sha256
    if not staged.exists():
        with TemporaryDirectory(dir=cache) as temporary:
            copy = Path(temporary) / "kernel"
            copy.mkdir(mode=0o777 if os.name == "nt" else 0o700)
            for name, limit in {**ARTIFACT_LIMITS, MANIFEST: 8192, SIGNATURE: 128}.items():
                with (
                    open_bundle_file(source / name, limit) as (origin, size),
                    (copy / name).open("xb") as target,
                ):
                    remaining = size
                    while remaining:
                        block = origin.read(min(1024**2, remaining))
                        if not block:
                            raise StateMismatchError(f"Kernel bundle changed while staging {name}")
                        target.write(block)
                        remaining -= len(block)
                    target.flush()
                    os.fsync(target.fileno())
            verify_bundle(copy, manifest_sha256=manifest_sha256, public_key=public_key)
            copy.rename(staged)
    verify_bundle(staged, manifest_sha256=manifest_sha256, public_key=public_key)
    raw = read_bundle_file(staged / MANIFEST, 8192)
    if _digest(raw) != manifest_sha256:
        raise StateMismatchError("Staged WSL manifest changed after verification")
    return parse_manifest(raw)


class WslKernelActivation:
    def __init__(self, host: WslOperatorHost, config: Path | None = None) -> None:
        self.host = host
        self.config = config if config is not None else Path.home() / ".wslconfig"
        self.cache = host.root / "wsl-kernel"
        self.journal = self.cache / "change.json"

    def _registration(self) -> str:
        record = self.host._read_receipt()
        if record["state"] != "ready":
            raise StateMismatchError("Prepare the managed WSL host before activating a kernel")
        return self.host._require_registration(_registered_distro(), record).identifier

    def _running(self) -> set[str]:
        value = run_host([self.host.wsl, "--list", "--running", "--quiet"])
        return {line.strip() for line in value.splitlines() if line.strip()}

    def _require_idle(self) -> None:
        if self._running():
            raise StateMismatchError(
                "Close all WSL distributions before a kernel maintenance restart"
            )

    def _shutdown(self) -> None:
        self._require_idle()
        run_host([self.host.wsl, "--shutdown"])

    def _boot(self, *, candidate: bool = False) -> dict[str, Any]:
        self._require_idle()
        args = ["-I", "-B", "-c", _BOOT_PROBE, "candidate" if candidate else "baseline"]
        try:
            output = run_host(self.host.command("/usr/bin/python3.12", args), timeout=90)
        finally:
            # The preflight required a stopped host. Only the owned probe distro
            # may be terminated here if the boot or the probe was interrupted.
            active = self._running()
            if DISTRO in active:
                run_host([self.host.wsl, "--terminate", DISTRO])
            if active - {DISTRO}:
                raise StateMismatchError(
                    "Another WSL distribution started during kernel maintenance"
                )
        try:
            value = json.loads(output)
            if (
                not isinstance(value, dict)
                or value.get("architecture") != "x86_64"
                or not _KERNEL_RELEASE.fullmatch(value["kernel_release"])
                or not _BOOT_ID.fullmatch(value["boot_id"])
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, RecursionError) as error:
            raise StateMismatchError("Managed WSL boot evidence is invalid") from error
        return value

    def _restore(self, change: _Change) -> dict[str, Any]:
        if self._registration() != change.registration:
            raise StateMismatchError("Managed WSL registration changed since kernel activation")
        current = _read_local(self.config)
        if current != change.original:
            if current is None or _digest(current) != change.staged_sha256:
                raise StateMismatchError(
                    "WSL configuration changed independently; retained rollback receipt requires reconciliation"
                )
            _replace_local(self.config, before=current, after=change.original)
        self._shutdown()
        boot = self._boot()
        if (
            boot["kernel_release"] != change.baseline_kernel
            or boot["boot_id"] == change.baseline_boot
            or _read_local(self.config) != change.original
        ):
            raise StateMismatchError("Original WSL configuration or kernel could not be verified")
        _replace_local(self.journal, before=change.encode(), after=None, limit=_JOURNAL_LIMIT)
        return self._result(change, boot, "rolled_back")

    @staticmethod
    def _result(change: _Change, boot: dict[str, Any], status: str) -> dict[str, Any]:
        return {
            "profile": "potbot.wsl-kernel-activation/1",
            "activation": status,
            "manifest_sha256": change.manifest_sha256,
            "baseline_kernel": change.baseline_kernel,
            "worker_qualification": "required",
            **boot,
        }

    def activate(self, bundle: Path, *, manifest_sha256: str, public_key: str) -> dict[str, Any]:
        with self.host._setup_session():
            registration = self._registration()
            self._require_idle()
            if _read_local(self.journal, _JOURNAL_LIMIT) is not None:
                raise StateMismatchError(
                    "Roll back the retained WSL kernel transaction before starting another"
                )
            original = _read_local(self.config)
            if not _hex(manifest_sha256, 64) or not _hex(public_key, 64):
                raise InvalidInputError(
                    "Select an exact WSL manifest SHA-256 and trusted Ed25519 public key"
                )
            candidate = self.cache / manifest_sha256
            updated = _configured(original, PureWindowsPath(candidate))
            create_directory(self.cache)
            if self.cache.resolve() != self.cache or _reparse(self.cache.lstat()):
                raise StateMismatchError("WSL kernel staging requires an ordinary local directory")
            manifest = _stage_bundle(
                bundle, self.cache, manifest_sha256=manifest_sha256, public_key=public_key
            )
            self._shutdown()
            baseline = self._boot()
            if baseline["kernel_release"] == manifest["kernel_release"]:
                raise StateMismatchError(
                    "The candidate kernel was already running with the original configuration"
                )
            if self._registration() != registration:
                raise StateMismatchError("Managed WSL registration changed during boot preflight")
            if _read_local(self.config) != original:
                raise StateMismatchError("WSL configuration changed during boot preflight")
            change = _Change(
                registration,
                original,
                _digest(updated),
                manifest_sha256,
                baseline["kernel_release"],
                baseline["boot_id"],
            )
            _replace_local(self.journal, before=None, after=change.encode(), limit=_JOURNAL_LIMIT)
            try:
                self._require_idle()
                _replace_local(self.config, before=original, after=updated)
                self._shutdown()
                boot = self._boot(candidate=True)
                if (
                    boot["kernel_release"] != manifest["kernel_release"]
                    or boot["boot_id"] == change.baseline_boot
                    or boot.get("config_sha256") != manifest["artifacts"]["kernel.config"]["sha256"]
                    or boot.get("module_release") != manifest["kernel_release"]
                    or _read_local(self.config) != updated
                    or self._registration() != registration
                ):
                    raise StateMismatchError(
                        "Booted WSL kernel, configuration or modules differ from the verified bundle"
                    )
            except (DomainError, OSError, ValueError, KeyboardInterrupt) as error:
                try:
                    self._restore(change)
                except (DomainError, OSError, ValueError) as rollback_error:
                    raise StateMismatchError(
                        "WSL kernel activation failed and rollback needs attention; the transaction receipt is retained"
                    ) from rollback_error
                raise StateMismatchError(
                    "WSL kernel activation failed; the original configuration and boot were verified"
                ) from error
            return self._result(change, boot, "boot_verified")

    def rollback(self) -> dict[str, Any]:
        with self.host._setup_session():
            self._require_idle()
            return self._restore(_Change.load(self.journal))
