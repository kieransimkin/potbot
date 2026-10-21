"""Disposable real Git repositories for controller and verification tests.

The factory owns one UUID-named child. Its parent and caller sentinel remain
caller-owned. Cleanup needs both an intact marker and the original ownership
token; a familiar-looking directory name alone never authorizes deletion.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from potbot.domain import DomainError, InvalidInputError, ScopeDeniedError

MARKER_NAME = ".potbot-fixture.json"
SENTINEL_NAME = "caller-owned-sentinel.txt"
FAULTY_LINE = "    return sum(values) + offset - 1\n"
REPAIRED_LINE = "    return sum(values) + offset\n"


class FixtureVariant(StrEnum):
    BASELINE = "baseline"
    DIRTY_TRACKED = "dirty_tracked"
    UNTRACKED_INPUT = "untracked_input"
    PRE_EXISTING_FAILURE = "pre_existing_failure"


@dataclass(frozen=True, slots=True)
class RepositoryFixture:
    path: Path
    ownership_token: str
    fixture_id: UUID
    variant: FixtureVariant
    baseline_commit: str | None
    left_commit: str | None
    right_commit: str | None

    @property
    def sentinel_path(self) -> Path:
        return self.path.parent / SENTINEL_NAME

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "path": str(self.path),
            "ownership_token": self.ownership_token,
            "fixture_id": str(self.fixture_id),
            "variant": self.variant.value,
            "baseline_commit": self.baseline_commit,
            "left_commit": self.left_commit,
            "right_commit": self.right_commit,
        }


def _checked_path(path: Path) -> Path:
    # abspath normalizes dot components without following symlinks.
    result = Path(os.path.abspath(path))
    for part in (result, *result.parents):
        if part.is_symlink():
            raise ScopeDeniedError("fixture paths cannot traverse symlinks")
    return result


def _git(path: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    # Fixtures must not inherit a developer's signing, hooks, or template setup.
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", *args],
        cwd=path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout.strip()


def _write_marker(fixture: RepositoryFixture) -> None:
    marker = fixture.path / MARKER_NAME
    temp = fixture.path / (MARKER_NAME + ".tmp")
    with temp.open("x", encoding="utf-8") as stream:
        json.dump(fixture.to_dict(), stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(marker)


def load_repository_fixture(path: Path, ownership_token: str) -> RepositoryFixture:
    """Validate ownership; partially initialized owned fixtures can be cleaned."""
    path = _checked_path(path)
    if not path.is_dir() or re.fullmatch(r"fixture-[0-9a-f]{32}", path.name) is None:
        raise ScopeDeniedError("target is not an owned fixture child")
    marker = path / MARKER_NAME
    if marker.is_symlink() or not marker.is_file():
        raise ScopeDeniedError("fixture ownership marker is missing or unsafe")
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
        expected = {
            "schema_version",
            "path",
            "ownership_token",
            "fixture_id",
            "variant",
            "baseline_commit",
            "left_commit",
            "right_commit",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid marker fields")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("unsupported marker version")
        fixture_id = UUID(value["fixture_id"])
        variant = FixtureVariant(value["variant"])
        if value["path"] != str(path) or path.name != f"fixture-{fixture_id.hex}":
            raise ValueError("fixture path does not match ownership record")
        token = value["ownership_token"]
        if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None:
            raise ValueError("invalid ownership token")
        if not isinstance(ownership_token, str) or not secrets.compare_digest(
            token, ownership_token
        ):
            raise ValueError("ownership token does not match")
        for key in ("baseline_commit", "left_commit", "right_commit"):
            if value[key] is not None and (
                not isinstance(value[key], str)
                or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value[key]) is None
            ):
                raise ValueError("invalid commit identity")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ScopeDeniedError("fixture ownership could not be established") from exc
    return RepositoryFixture(
        path,
        token,
        fixture_id,
        variant,
        value["baseline_commit"],
        value["left_commit"],
        value["right_commit"],
    )


def _validate(fixture: RepositoryFixture) -> RepositoryFixture:
    loaded = load_repository_fixture(fixture.path, fixture.ownership_token)
    if loaded != fixture:
        raise ScopeDeniedError("fixture handle does not match its ownership record")
    return loaded


def create_repository_fixture(
    parent: Path,
    variant: FixtureVariant | str = FixtureVariant.BASELINE,
) -> RepositoryFixture:
    """Create one repository; each instance has an independent initial history."""
    try:
        variant = FixtureVariant(variant)
    except (ValueError, TypeError) as exc:
        raise InvalidInputError("unknown repository fixture variant") from exc
    parent = _checked_path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    parent = _checked_path(parent)
    if not parent.is_dir():
        raise InvalidInputError("fixture parent must be a directory")
    sentinel = parent / SENTINEL_NAME
    if sentinel.is_symlink():
        raise ScopeDeniedError("caller sentinel must not be a symlink")
    try:
        with sentinel.open("x", encoding="utf-8") as stream:
            stream.write("Caller-owned sentinel. Fixture cleanup must preserve this file.\n")
    except FileExistsError:
        if not sentinel.is_file():
            raise ScopeDeniedError("caller sentinel is not a regular file") from None
    fixture_id = uuid4()
    path = parent / f"fixture-{fixture_id.hex}"
    path.mkdir()
    fixture = RepositoryFixture(path, secrets.token_hex(32), fixture_id, variant, None, None, None)
    _write_marker(fixture)
    try:
        _git(path, "init", "--template=", "--initial-branch=main", ".")
        _git(path, "config", "user.name", "potbot Fixture")
        _git(path, "config", "user.email", "fixture@example.invalid")
        _git(path, "config", "commit.gpgSign", "false")
        _git(path, "config", "core.autocrlf", "false")
        (path / "tests").mkdir()
        (path / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (path / ".gitignore").write_text(
            f"__pycache__/\n*.pyc\n{MARKER_NAME}\n{MARKER_NAME}.tmp\n",
            encoding="utf-8",
        )
        (path / "fixture_identity.txt").write_text(str(fixture_id) + "\n", encoding="utf-8")
        (path / "calculator.py").write_text(
            '"""Small intentionally defective program used only by test fixtures."""\n'
            "from pathlib import Path\n\n"
            "def total(values):\n"
            "    runtime_input = Path(__file__).with_name('runtime_offset.txt')\n"
            "    offset = int(runtime_input.read_text()) if runtime_input.exists() else 0\n"
            + FAULTY_LINE,
            encoding="utf-8",
        )
        (path / "tests" / "test_calculator.py").write_text(
            "import unittest\nfrom calculator import total\n\n"
            "class CalculatorTests(unittest.TestCase):\n"
            "    def test_total(self):\n        self.assertEqual(total([2, 3]), 5)\n"
            "    def test_empty_total(self):\n        self.assertEqual(total([]), 0)\n",
            encoding="utf-8",
        )
        (path / "REPAIR.md").write_text(
            "# Fixture repair\n\nRemove the erroneous `- 1` in calculator.total. "
            "Preserve runtime_offset.txt handling and any other local changes. "
            "Run `python -m unittest discover -s tests -t . -v`. "
            "The pre_existing_failure variant intentionally keeps an unrelated failing test.\n",
            encoding="utf-8",
        )
        if variant is FixtureVariant.PRE_EXISTING_FAILURE:
            (path / "tests" / "test_preexisting.py").write_text(
                "import unittest\n\nclass PreExistingFailure(unittest.TestCase):\n"
                "    def test_unrelated_known_failure(self):\n"
                "        self.fail('intentional pre-existing fixture failure')\n",
                encoding="utf-8",
            )
        _git(path, "add", ".")
        _git(path, "commit", "-m", "Fixture faulty baseline")
        baseline = _git(path, "rev-parse", "HEAD")
        _git(path, "checkout", "-b", "candidate-left", baseline)
        (path / "branch_note.txt").write_text("left branch observation\n", encoding="utf-8")
        _git(path, "add", "branch_note.txt")
        _git(path, "commit", "-m", "Fixture left branch")
        left = _git(path, "rev-parse", "HEAD")
        _git(path, "checkout", "-b", "candidate-right", baseline)
        (path / "branch_note.txt").write_text("right branch observation\n", encoding="utf-8")
        _git(path, "add", "branch_note.txt")
        _git(path, "commit", "-m", "Fixture right branch")
        right = _git(path, "rev-parse", "HEAD")
        _git(path, "checkout", "main")
        if variant is FixtureVariant.DIRTY_TRACKED:
            with (path / "calculator.py").open("a", encoding="utf-8") as stream:
                stream.write("\n# Deliberate local tracked change that snapshots must preserve.\n")
        elif variant is FixtureVariant.UNTRACKED_INPUT:
            (path / "runtime_offset.txt").write_text("0\n", encoding="utf-8")
        fixture = RepositoryFixture(
            path, fixture.ownership_token, fixture_id, variant, baseline, left, right
        )
        _write_marker(fixture)
        return fixture
    except BaseException:
        # Keep the initial ownership marker for recovery after interruption.
        # No broad parent cleanup is attempted here.
        raise


def repair_repository_fixture(fixture: RepositoryFixture) -> bool:
    """Apply the documented one-line repair, preserving other local changes."""
    _validate(fixture)
    source = _checked_path(fixture.path / "calculator.py")
    text = source.read_text(encoding="utf-8")
    if text.count(FAULTY_LINE) == 1:
        source.write_text(text.replace(FAULTY_LINE, REPAIRED_LINE, 1), encoding="utf-8")
        return True
    if FAULTY_LINE not in text and text.count(REPAIRED_LINE) == 1:
        return False
    raise InvalidInputError("fixture source no longer matches the documented repair")


def check_repository_fixture(fixture: RepositoryFixture) -> subprocess.CompletedProcess[str]:
    """Run real behavioral tests. A faulty baseline intentionally returns 1."""
    _validate(fixture)
    return subprocess.run(
        [sys.executable, "-I", "-B", "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"],
        cwd=fixture.path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def cleanup_repository_fixture(fixture: RepositoryFixture) -> None:
    """Delete only the validated child, leaving parent and sibling paths intact."""
    _validate(fixture)
    shutil.rmtree(fixture.path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="create a marked disposable Git fixture")
    create.add_argument("--parent", type=Path, required=True)
    create.add_argument("--variant", choices=[x.value for x in FixtureVariant], default="baseline")
    for name in ("check", "repair", "cleanup"):
        operation = sub.add_parser(name)
        operation.add_argument("path", type=Path)
        operation.add_argument("--token", required=True, help="ownership token returned by create")
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            print(
                json.dumps(
                    create_repository_fixture(args.parent, args.variant).to_dict(), sort_keys=True
                )
            )
            return 0
        fixture = load_repository_fixture(args.path, args.token)
        if args.command == "check":
            result = check_repository_fixture(fixture)
            print(
                json.dumps(
                    {
                        "returncode": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    }
                )
            )
            return result.returncode
        if args.command == "repair":
            print(json.dumps({"changed": repair_repository_fixture(fixture)}))
        elif args.command == "cleanup":
            cleanup_repository_fixture(fixture)
            print(json.dumps({"removed": str(fixture.path)}))
        return 0
    except DomainError as exc:
        print(json.dumps(exc.to_dict()), file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError) as exc:
        # Do not dump process environment or unbounded subprocess diagnostics.
        print(
            json.dumps({"code": "fixture_operation_failed", "message": type(exc).__name__}),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
