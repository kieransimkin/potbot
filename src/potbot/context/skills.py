"""Operator-only immutable skill releases for /usr/local/skills.

The backing source is never guest-writable: agents propose SKILL.md through an
admitted controller operation. A release is published by atomic directory rename
and cannot replace an existing release. Activation/version pinning is separate.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from potbot.context.model import document_templates, sha256, validate_content
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError

_SKILL_NAME = re.compile(r"[a-z][a-z0-9-]{1,62}\Z")
_ENTRY_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,80}\Z")


def validate_skill(name: str, skill_md: str) -> None:
    if not isinstance(name, str) or not _SKILL_NAME.fullmatch(name):
        raise InvalidInputError("Skill name must be an approved canonical slug")
    validate_content(skill_md)
    if not skill_md.startswith("---\n"):
        raise InvalidInputError("SKILL.md requires YAML frontmatter")
    end = skill_md.find("\n---\n", 4)
    if end < 0 or end > 8192:
        raise InvalidInputError("SKILL.md frontmatter must be bounded and terminated")
    fields = skill_md[4:end].splitlines()
    # Strict enough to prevent accidental broken specification, without
    # pretending to parse a general-purpose YAML document using ad-hoc regex.
    if not any(line.startswith("name:") for line in fields) or not any(
        line.startswith("description:") for line in fields
    ):
        raise InvalidInputError("SKILL.md must declare name and description")
    for line in fields:
        if line.startswith("name:") and line.partition(":")[2].strip().strip("'\"") != name:
            raise InvalidInputError("SKILL.md name must equal its canonical directory name")


@dataclass(frozen=True, slots=True)
class SkillRelease:
    name: str
    sha256: str
    path: Path
    manifest_sha256: str


class SharedSkillReleaseStore:
    """Immutable, operator-owned release directories; no guest mount mutation."""

    def __init__(self, root: Path) -> None:
        raw = Path(root)
        if not raw.is_absolute() or raw.is_symlink():
            raise ScopeDeniedError("Skill release root must be an absolute real directory")
        try:
            info = raw.stat(follow_symlinks=False)
        except OSError as exc:
            raise ScopeDeniedError("Skill release root does not exist") from exc
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
            raise ScopeDeniedError("Skill release root must be operator controlled")
        self.root = raw.resolve(strict=True)

    @staticmethod
    def _write(directory: Path, name: str, data: bytes) -> None:
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_NOFOLLOW | os.O_CLOEXEC, 0o400)
        try:
            with os.fdopen(fd, "wb", closefd=False) as out:
                out.write(data)
                out.flush()
            os.fchmod(fd, 0o444)
            os.fsync(fd)
        finally:
            os.close(fd)

    def publish(self, name: str, skill_md: str) -> SkillRelease:
        validate_skill(name, skill_md)
        raw = skill_md.encode("utf-8")
        version = hashlib.sha256(raw).hexdigest()
        # Initialize operator-controlled parent once, with no symlink traversal.
        slug = self.root / name
        try:
            slug.mkdir(mode=0o755)
        except FileExistsError:
            pass
        if slug.is_symlink() or not slug.is_dir() or slug.resolve(strict=True).parent != self.root:
            raise ScopeDeniedError("Skill release namespace was redirected")
        if slug.stat().st_mode & 0o022:
            raise ScopeDeniedError("Skill release namespace is guest-writable")
        releases = slug / "releases"
        try:
            releases.mkdir(mode=0o755)
        except FileExistsError:
            pass
        if (releases.is_symlink() or not releases.is_dir()
                or releases.resolve(strict=True).parent != slug
                or releases.stat().st_mode & 0o022):
            raise ScopeDeniedError("Skill releases are not in a trusted real directory")
        manifest = {"schema": "potbot-skill-release/v1", "skill": name, "sha256": version}
        manifest_raw = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
        target = releases / version
        if target.exists() or target.is_symlink():
            self._verify(target, raw, manifest_raw)
            return SkillRelease(name, version, target, hashlib.sha256(manifest_raw).hexdigest())
        temporary = Path(tempfile.mkdtemp(prefix=".publishing-", dir=releases))
        try:
            self._write(temporary, "SKILL.md", raw)
            for role, content in document_templates("skill", name).items():
                self._write(temporary, role, content.encode("utf-8"))
            self._write(temporary, "release.json", manifest_raw)
            os.chmod(temporary, 0o555)
            # The release digest uniquely names the bytes. os.rename cannot
            # replace an existing *nonempty* directory on Linux. Concurrent
            # duplicate release is verified and treated as an idempotent call.
            try:
                os.rename(temporary, target)
            except OSError:
                if not target.is_dir() or target.is_symlink():
                    raise
                self._verify(target, raw, manifest_raw)
            else:
                self._verify(target, raw, manifest_raw)
            return SkillRelease(name, version, target, hashlib.sha256(manifest_raw).hexdigest())
        finally:
            if temporary.exists():
                os.chmod(temporary, 0o700)
                for path in temporary.iterdir():
                    path.unlink()
                temporary.rmdir()

    @staticmethod
    def _verify(directory: Path, skill_md: bytes, manifest: bytes) -> None:
        if directory.is_symlink() or not directory.is_dir():
            raise ScopeDeniedError("Published skill directory is not a real directory")
        expected = {"SKILL.md": skill_md, "release.json": manifest}
        for name, payload in expected.items():
            file = directory / name
            fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o222:
                    raise ScopeDeniedError("Skill release contains a mutable or linked file")
                if info.st_size != len(payload) or os.read(fd, len(payload) + 1) != payload:
                    raise StateMismatchError("Published skill release digest differs")
            finally:
                os.close(fd)
        for role, content in document_templates("skill", directory.parent.parent.name).items():
            actual = directory / role
            if actual.is_symlink() or not actual.is_file() or actual.stat().st_mode & 0o222:
                raise ScopeDeniedError("Skill release context was replaced")
            if sha256(actual.read_text(encoding="utf-8")) != sha256(content):
                raise StateMismatchError("Skill context does not match immutable release")
