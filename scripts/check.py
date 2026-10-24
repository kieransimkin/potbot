#!/usr/bin/env python3
"""Run reproducible application gates and write sanitized failure diagnostics."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from potbot.evaluation.diagnostics import sanitize_text
from potbot.tools.temporary import TemporaryDirectory, allocate_file

ROOT = Path(__file__).resolve().parents[1]


class CheckFailed(RuntimeError):
    """A named check failed after its sanitized diagnostics were saved."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-postgres", action="store_true")
    parser.add_argument("--with-sandbox", action="store_true")
    parser.add_argument("--artifacts", type=Path, default=ROOT / "artifacts")
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []
    started_at = datetime.now(UTC).isoformat()
    # Only successful completion of every requested gate may mark this run green.
    status = 1
    process_scratch: TemporaryDirectory | None = None

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
                env=process_scratch.environment(environment) if process_scratch else environment,
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
        process_scratch = TemporaryDirectory(
            prefix="potbot-check-process-", dir=ROOT / ".potbot/temporary"
        )
        cache = str(process_scratch.path / "ruff-cache")
        run("lint", [sys.executable, "-m", "ruff", "check", "--cache-dir", cache, "."])
        run(
            "format", [sys.executable, "-m", "ruff", "format", "--cache-dir", cache, "--check", "."]
        )
        run(
            "types",
            [sys.executable, "-m", "mypy", "--cache-dir", str(process_scratch.path / "mypy-cache")],
        )
        run(
            "hierarchy-assets",
            [sys.executable, str(ROOT / "scripts/generate_type_hierarchy.py"), "--check"],
        )
        run(
            "agent-security-assets",
            [sys.executable, str(ROOT / "scripts/generate_agent_security.py"), "--check"],
        )
        with TemporaryDirectory(prefix="potbot-check-", dir=process_scratch.path) as temporary:
            temp = Path(temporary)
            junit = temp / "tests.xml"

            def tests(label: str, selector: str) -> None:
                try:
                    run(
                        label,
                        [sys.executable, "-m", "pytest", "-m", selector, f"--junitxml={junit}"],
                        timeout=600 if label == "postgres" else 240,
                    )
                finally:
                    if junit.exists():
                        (artifacts / f"{label}.xml").write_text(
                            sanitize_text(junit.read_text(encoding="utf-8")), encoding="utf-8"
                        )
                        junit.unlink()

            tests("unit-and-git", "not postgres and not sandbox")
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
            run(
                "installed-execution-help",
                [str(clean_cli), "execution", "--help"],
                cwd=temp,
                environment=clean_environment,
            )
            for command in (
                "patch",
                "credentials",
                "dependencies",
                "models",
                "serve",
                "jobs",
                "identity",
            ):
                run(
                    f"installed-{command}-help",
                    [str(clean_cli), command, "--help"],
                    cwd=temp,
                    environment=clean_environment,
                )
            run(
                "installed-identity-resources",
                [
                    str(clean_python),
                    "-I",
                    "-c",
                    "from pathlib import Path; import sys; "
                    "from potbot.security.broker import native_source_directory; "
                    "from potbot.semantics.agent_security import security_type_snapshot; "
                    "from potbot.semantics.catalog import merge_types; "
                    "source = native_source_directory(); "
                    "assert source.is_relative_to(Path(sys.prefix).resolve()); "
                    "assert (source / 'Cargo.lock').is_file(); "
                    "assert (source / 'src' / 'secure.rs').is_file(); "
                    "assert len(merge_types(security_type_snapshot())) == 715; "
                    "assert 'cryptography' not in sys.modules; "
                    "assert 'psycopg' not in sys.modules; "
                    "print('installed native sources and Agent security catalog verified')",
                ],
                cwd=temp,
                environment=clean_environment,
            )
            run(
                "installed-semantic-catalog",
                [
                    str(clean_python),
                    "-I",
                    "-c",
                    "from potbot.semantics.registry import SemanticRegistry; "
                    "registry = SemanticRegistry.load(); "
                    "assert len(registry.inventory('data_type')) == 45; "
                    "assert len(registry.inventory('retrieval_profile')) == 7; "
                    "print('installed semantic catalog verified: ' + registry.fingerprint)",
                ],
                cwd=temp,
                environment=clean_environment,
            )
            run(
                "installed-hierarchy",
                [str(clean_cli), "syn", "hierarchy", "--summary"],
                cwd=temp,
                environment=clean_environment,
            )
            if args.with_postgres:
                # Verify private recovery storage before acquiring a DB capability.
                # Keep 0700 custody: a restricted host must fail before DB creation.
                recovery = ROOT / ".test-runs"
                recovery.mkdir(mode=0o700, exist_ok=True)
                descriptor, probe = allocate_file(recovery, prefix=".access-probe-")
                os.close(descriptor)
                probe.unlink()
                manifest = temp / "migration-run.json"
                db_command = [sys.executable, "-m", "potbot", "db"]
                try:
                    run("db-bootstrap", [*db_command, "bootstrap"])
                    run("db-create", [*db_command, "create", "--manifest", str(manifest)])
                    run("db-migrate", [*db_command, "migrate", "--manifest", str(manifest)])
                    run("db-check", [*db_command, "check", "--manifest", str(manifest)])
                    tests("postgres", "postgres and not sandbox")
                finally:
                    if manifest.exists():
                        try:
                            run("db-cleanup", [*db_command, "cleanup", "--manifest", str(manifest)])
                        except BaseException:
                            # Keep the capability outside uploaded diagnostics for manual recovery.
                            descriptor, target = allocate_file(
                                recovery, prefix="recovery-", suffix=".json"
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
            if args.with_sandbox:
                tests("sandbox", "sandbox" if args.with_postgres else "sandbox and not postgres")
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
        if process_scratch is not None:
            try:
                process_scratch.cleanup()
            except OSError as error:
                print(sanitize_text(str(error)), file=sys.stderr)
                results.append({"check": "scratch-cleanup", "exit_code": 1})
                if status == 0:
                    status = 1
        summary = {
            "schema_version": 1,
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            "postgres_requested": args.with_postgres,
            "sandbox_requested": args.with_sandbox,
            "success": status == 0,
            "checks": results,
        }
        (artifacts / "summary.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
