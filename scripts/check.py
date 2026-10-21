#!/usr/bin/env python3
"""Run reproducible application gates and write sanitized failure diagnostics."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from potbot.evaluation.diagnostics import sanitize_text

ROOT = Path(__file__).resolve().parents[1]


class CheckFailed(RuntimeError):
    """A named check failed after its sanitized diagnostics were saved."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-postgres", action="store_true")
    parser.add_argument("--artifacts", type=Path, default=ROOT / "artifacts")
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []
    started_at = datetime.now(UTC).isoformat()
    # Only successful completion of every requested gate may mark this run green.
    status = 1

    def run(
        label: str,
        command: list[str],
        *,
        cwd: Path = ROOT,
        timeout: int = 240,
        environment: dict[str, str] | None = None,
    ) -> None:
        print(f"Running {label}...", flush=True)
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
            output = completed.stdout + completed.stderr
            returncode = completed.returncode
        except subprocess.TimeoutExpired as error:
            output = f"Check exceeded {timeout} seconds.\n"
            for partial in (error.stdout, error.stderr):
                if partial:
                    output += (
                        partial.decode(errors="replace") if isinstance(partial, bytes) else partial
                    )
            returncode = 124
        except OSError as error:
            output = f"Could not start check: {error}\n"
            returncode = 127
        clean_output = sanitize_text(output)
        (artifacts / f"{label}.log").write_text(clean_output, encoding="utf-8")
        print(clean_output, end="" if clean_output.endswith("\n") else "\n", flush=True)
        results.append({"check": label, "exit_code": returncode})
        if returncode:
            raise CheckFailed(f"{label} failed (exit {returncode})")

    try:
        run("lint", [sys.executable, "-m", "ruff", "check", "."])
        run("format", [sys.executable, "-m", "ruff", "format", "--check", "."])
        run("types", [sys.executable, "-m", "mypy"])
        with tempfile.TemporaryDirectory(prefix="potbot-check-") as temporary:
            temp = Path(temporary)
            junit = temp / "tests.xml"

            def tests(label: str, selector: str) -> None:
                try:
                    run(
                        label,
                        [sys.executable, "-m", "pytest", "-m", selector, f"--junitxml={junit}"],
                    )
                finally:
                    if junit.exists():
                        (artifacts / f"{label}.xml").write_text(
                            sanitize_text(junit.read_text(encoding="utf-8")), encoding="utf-8"
                        )
                        junit.unlink()

            tests("unit-and-git", "not postgres")
            run("wheel-build", ["uv", "build", "--wheel", "--out-dir", str(temp / "dist")])
            wheels = list((temp / "dist").glob("*.whl"))
            if len(wheels) != 1:
                raise CheckFailed("Expected one application wheel")
            clean_env = temp / "clean-env"
            run("wheel-environment", ["uv", "venv", "--python", sys.executable, str(clean_env)])
            bin_dir = clean_env / ("Scripts" if os.name == "nt" else "bin")
            clean_python = bin_dir / ("python.exe" if os.name == "nt" else "python")
            clean_cli = bin_dir / ("potbot.exe" if os.name == "nt" else "potbot")
            clean_environment = {
                name: value
                for name, value in os.environ.items()
                if name not in {"PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE"}
            }
            clean_environment["PYTHONNOUSERSITE"] = "1"
            run(
                "wheel-install",
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(clean_python),
                    "--no-deps",
                    str(wheels[0]),
                ],
            )
            run(
                "installed-version",
                [str(clean_cli), "--version"],
                cwd=temp,
                environment=clean_environment,
            )
            run(
                "installed-help",
                [str(clean_cli), "--help"],
                cwd=temp,
                environment=clean_environment,
            )
            run(
                "installed-domain",
                [
                    str(clean_python),
                    "-I",
                    "-c",
                    "from pathlib import Path; import sys, potbot, potbot.domain; "
                    "assert Path(potbot.__file__).resolve().is_relative_to("
                    "Path(sys.prefix).resolve()), 'package imported outside the clean environment'; "
                    "print('installed domain import passed')",
                ],
                cwd=temp,
                environment=clean_environment,
            )
            if args.with_postgres:
                manifest = temp / "migration-run.json"
                db_command = [sys.executable, "-m", "potbot", "db"]
                try:
                    run("db-bootstrap", [*db_command, "bootstrap"])
                    run("db-create", [*db_command, "create", "--manifest", str(manifest)])
                    run("db-migrate", [*db_command, "migrate", "--manifest", str(manifest)])
                    run("db-check", [*db_command, "check", "--manifest", str(manifest)])
                    tests("postgres", "postgres")
                finally:
                    if manifest.exists():
                        try:
                            run("db-cleanup", [*db_command, "cleanup", "--manifest", str(manifest)])
                        except BaseException:
                            # Keep the capability outside uploaded diagnostics for manual recovery.
                            recovery = ROOT / ".test-runs"
                            recovery.mkdir(mode=0o700, exist_ok=True)
                            descriptor, target = tempfile.mkstemp(
                                prefix="recovery-", suffix=".json", dir=recovery
                            )
                            with (
                                os.fdopen(descriptor, "wb") as output,
                                manifest.open("rb") as source,
                            ):
                                shutil.copyfileobj(source, output)
                                output.flush()
                                os.fsync(output.fileno())
                            print(
                                f"Cleanup failed; recovery manifest retained at {target}",
                                flush=True,
                            )
                            raise
        status = 0
    except KeyboardInterrupt:
        print("Check runner interrupted.", file=sys.stderr)
        status = 130
    except (CheckFailed, OSError) as error:
        print(sanitize_text(str(error)), file=sys.stderr)
    except Exception as error:
        print(
            sanitize_text(f"Unexpected check runner error ({type(error).__name__}): {error}"),
            file=sys.stderr,
        )
    finally:
        summary = {
            "schema_version": 1,
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            "postgres_requested": args.with_postgres,
            "success": status == 0,
            "checks": results,
        }
        (artifacts / "summary.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
