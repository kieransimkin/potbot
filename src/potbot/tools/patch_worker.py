"""Trusted bounded patch adapter; invoked only inside the qualified tool sandbox."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

MAX_INPUT = 6 * 1024 * 1024
MAX_PATCH = 4 * 1024 * 1024


class PatchRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code, self.message = code, message
        super().__init__(message)


def _path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2000
        or value.startswith("/")
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(part in {"", ".", ".."} or part.casefold() == ".git" for part in value.split("/"))
    ):
        raise PatchRejected("scope_denied", "Patch includes an unsafe repository path.")
    return value


def _safe_file(root: Path, relative: str) -> None:
    current = root
    parts = relative.split("/")
    for index, part in enumerate(parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode) or not (
            stat.S_ISDIR(mode) if index < len(parts) - 1 else stat.S_ISREG(mode)
        ):
            raise PatchRejected("scope_denied", "Patch cannot traverse links or special files.")


def apply_payload(payload: object, root: Path) -> list[str]:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "patch_base64", "permitted_paths"}
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
    ):
        raise PatchRejected("schema_violation", "Unsupported patch application envelope.")
    scopes = payload["permitted_paths"]
    if not isinstance(scopes, list) or not 1 <= len(scopes) <= 128:
        raise PatchRejected("schema_violation", "Patch scope must be a bounded path list.")
    scopes = [_path(path) for path in scopes]
    try:
        patch = base64.b64decode(payload["patch_base64"], validate=True)
        patch.decode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise PatchRejected("schema_violation", "Patch bytes are invalid.") from None
    if not 1 <= len(patch) <= MAX_PATCH or b"\x00" in patch:
        raise PatchRejected("schema_violation", "Patch exceeds the supported text format bounds.")
    # The first adapter deliberately supports ordinary text add/change/delete.
    # Reject both source/destination move/copy effects and nonregular modes.
    for line in patch.splitlines():
        if line.startswith(
            (
                b"rename from ",
                b"rename to ",
                b"copy from ",
                b"copy to ",
                b"GIT binary patch",
                b"Binary files ",
            )
        ):
            raise PatchRejected(
                "schema_violation", "Renames, copies and binary patches are unsupported."
            )
        match = re.match(
            rb"(?:new file mode |deleted file mode |old mode |new mode |index [0-9a-f]+\.\.[0-9a-f]+ )([0-9]{6})$",
            line,
        )
        if match is not None and match.group(1) not in {b"100644", b"100755"}:
            raise PatchRejected(
                "scope_denied", "Patch cannot create links, submodules or special files."
            )
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        raise PatchRejected("capability_unavailable", "Git patch application is unavailable.")
    command = [
        executable,
        "--no-optional-locks",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "protocol.allow=never",
    ]
    if (root / ".git").exists() or (root / ".git").is_symlink():
        raise PatchRejected(
            "scope_denied", "Patch candidates must not contain Git administration or configuration."
        )
    environment = {
        "PATH": os.defpath,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        # Git does not treat its initial cwd as a discovery ceiling. Stop at
        # its parent so workspace-local scratch cannot inherit an outer repo.
        "GIT_CEILING_DIRECTORIES": str(root.resolve().parent),
    }

    def git(*arguments: str) -> bytes:
        result = subprocess.run(
            [*command, "apply", *arguments, "-"],
            cwd=root,
            input=patch,
            env=environment,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            raise PatchRejected(
                "state_mismatch", "Patch does not apply cleanly to the declared base."
            )
        return result.stdout

    def changed_paths(changes: bytes) -> list[str]:
        paths = []
        for change in changes.split(b"\x00"):
            if not change:
                continue
            fields = change.split(b"\t", 2)
            if len(fields) != 3 or not all(item.isdigit() for item in fields[:2]):
                raise PatchRejected(
                    "schema_violation", "Patch has an unsupported file-change format."
                )
            try:
                path = _path(fields[2].decode("utf-8"))
            except UnicodeError:
                raise PatchRejected("schema_violation", "Patch paths must be UTF-8.") from None
            if not any(path == scope or path.startswith(scope + "/") for scope in scopes):
                raise PatchRejected(
                    "scope_denied", "Patch changes a path outside its declared scope."
                )
            _safe_file(root, path)
            paths.append(path)
        return paths

    paths = changed_paths(git("--numstat", "-z"))
    # Reverse parsing exposes source paths as well as destinations. It also
    # covers delete patches and unusual old/new headers without trusting our
    # own interpretation of Git's quoting and pathname syntax.
    changed_paths(git("--numstat", "-z", "--reverse"))
    if not paths or len(paths) > 128 or len(set(paths)) != len(paths):
        raise PatchRejected("schema_violation", "Patch must name distinct bounded changed files.")
    git("--check", "--whitespace=nowarn")
    git("--whitespace=nowarn")
    return paths


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise PatchRejected("schema_violation", "Patch request exceeds the supported size.")
        paths = apply_payload(json.loads(raw), Path.cwd())
        print(
            json.dumps(
                {"schema_version": 1, "status": "applied", "changed_paths": paths}, sort_keys=True
            )
        )
        return 0
    except PatchRejected as error:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "rejected",
                    "error_code": error.code,
                    "message": error.message,
                },
                sort_keys=True,
            )
        )
        return 2
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "rejected",
                    "error_code": "tool_failure",
                    "message": "Patch adapter could not complete the operation.",
                },
                sort_keys=True,
            )
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
