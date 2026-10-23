from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def launcher(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    if os.name == "nt" or shutil.which("bash") is None:
        pytest.skip(
            "Bash launcher contract runs on POSIX; native Windows has a separate smoke test"
        )
    project = tmp_path / "project with spaces"
    project.mkdir()
    shutil.copyfile(ROOT / "start.sh", project / "start.sh")
    syn_manifest = project / "vendor/syn/packages/syn-client/pyproject.toml"
    syn_manifest.parent.mkdir(parents=True)
    syn_manifest.write_text("# Complete-distribution fixture\n")
    binaries = tmp_path / "bin with spaces"
    binaries.mkdir()
    fake_uv = binaries / "uv"
    fake_uv.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('uv 0.12.23 (test runtime)'); sys.exit(0)\n"
        "record = {'args': sys.argv[1:], 'cwd': os.getcwd(), 'env': {\n"
        "    key: os.environ.get(key) for key in ['POTBOT_PROJECT_ROOT', 'UV_PROJECT_ENVIRONMENT',\n"
        "    'UV_CACHE_DIR', 'UV_PYTHON_INSTALL_DIR']}}\n"
        "if sys.argv[1] == 'run':\n"
        "    record['stdin'] = sys.stdin.read()\n"
        "with open(os.environ['POTBOT_LAUNCH_LOG'], 'a') as log:\n"
        "    log.write(json.dumps(record) + '\\n')\n"
        "if sys.argv[1] == os.environ.get('POTBOT_FAIL_STAGE'):\n"
        "    sys.exit(int(os.environ.get('POTBOT_FAIL_CODE', '19')))\n"
        "if sys.argv[1] == 'run':\n"
        "    print('shared startup reached')\n"
        "    sys.exit(int(os.environ.get('POTBOT_APPLICATION_CODE', '0')))\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    log = tmp_path / "launch.jsonl"
    environment = {
        **os.environ,
        "PATH": str(binaries) + os.pathsep + os.environ.get("PATH", ""),
        "POTBOT_LAUNCH_LOG": str(log),
    }
    for key in (
        "UV_CACHE_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "UV_PROJECT_ENVIRONMENT",
        "POTBOT_PROJECT_ROOT",
    ):
        environment.pop(key, None)
    return project, environment, log


def test_missing_syn_source_fails_before_runtime_downloads(launcher):
    project, environment, log = launcher
    (project / "vendor/syn/packages/syn-client/pyproject.toml").unlink()
    result = subprocess.run(
        ["bash", str(project / "start.sh"), "--help"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 1
    assert "Syn submodule is missing" in result.stderr
    assert not log.exists()


def test_bash_launcher_installs_locked_extras_preserves_args_stdin_and_exit_code(
    tmp_path: Path,
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    project, environment, log = launcher
    environment["POTBOT_APPLICATION_CODE"] = "23"
    arguments = [
        "dependencies",
        "status",
        "two words",
        "",
        'a"b',
        "trailing\\",
        "$(false); & literal",
    ]
    result = subprocess.run(
        ["bash", str(project / "start.sh"), *arguments],
        cwd=tmp_path,
        env=environment,
        input="test stdin\n",
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 23
    assert result.stdout == "shared startup reached\n"
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert records[0]["args"] == ["python", "install", "3.12", "--no-bin", "--no-registry"]
    assert records[1]["args"] == [
        "sync",
        "--project",
        str(project),
        "--locked",
        "--all-extras",
        "--no-dev",
        "--python",
        "3.12",
        "--managed-python",
    ]
    assert records[2]["args"] == [
        "run",
        "--project",
        str(project),
        "--no-sync",
        "python",
        "-m",
        "potbot.startup",
        *arguments,
    ]
    assert records[2]["stdin"] == "test stdin\n"
    assert records[2]["cwd"] == str(tmp_path)
    assert records[2]["env"] == {
        "POTBOT_PROJECT_ROOT": str(project),
        "UV_PROJECT_ENVIRONMENT": str(project / ".venv"),
        "UV_CACHE_DIR": str(project / ".potbot/cache/uv"),
        "UV_PYTHON_INSTALL_DIR": str(project / ".potbot/runtime/python"),
    }
    assert all(f"{stage}/4" in result.stderr for stage in range(1, 5))


@pytest.mark.parametrize("stage,expected_calls", [("python", 1), ("sync", 2)])
def test_launcher_stops_on_dependency_error_without_starting_application(
    tmp_path: Path,
    launcher: tuple[Path, dict[str, str], Path],
    stage: str,
    expected_calls: int,
) -> None:
    project, environment, log = launcher
    environment.update(POTBOT_FAIL_STAGE=stage, POTBOT_FAIL_CODE="31")
    result = subprocess.run(
        ["bash", str(project / "start.sh"), "--help"],
        cwd=tmp_path,
        env=environment,
        input="",
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 31
    assert len(log.read_text().splitlines()) == expected_calls
    assert "Starting Potbot" not in result.stderr


def test_existing_local_uv_is_used_and_cache_preferences_are_honored(
    tmp_path: Path,
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    project, environment, log = launcher
    local_uv = project / ".potbot/runtime/uv/uv"
    local_uv.parent.mkdir(parents=True)
    source_uv = Path(environment["PATH"].split(os.pathsep)[0]) / "uv"
    shutil.move(source_uv, local_uv)
    environment["UV_CACHE_DIR"] = str(tmp_path / "shared cache")
    result = subprocess.run(
        ["bash", str(project / "start.sh"), "--help"],
        env=environment,
        input="",
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert records[-1]["env"]["UV_CACHE_DIR"] == str(tmp_path / "shared cache")
    assert "Downloading uv" not in result.stderr


def test_missing_pinned_uv_bootstraps_before_python_and_package_installation(
    tmp_path: Path,
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    project, environment, log = launcher
    binaries = Path(environment["PATH"].split(os.pathsep)[0])
    qualified_uv = tmp_path / "qualified uv fixture"
    shutil.copyfile(binaries / "uv", qualified_uv)
    (binaries / "uv").write_text((binaries / "uv").read_text().replace("0.12.23", "0.0.0"))
    environment["POTBOT_TEST_UV_SOURCE"] = str(qualified_uv)
    bootstrap_log = tmp_path / "bootstrap.json"
    environment["POTBOT_BOOTSTRAP_LOG"] = str(bootstrap_log)
    curl = binaries / "curl"
    curl.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['POTBOT_BOOTSTRAP_LOG']).write_text(json.dumps(sys.argv[1:]))\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "output.write_text('#!/bin/sh\\n'\n"
        '    \'cp -- "$POTBOT_TEST_UV_SOURCE" "$UV_UNMANAGED_INSTALL/uv"\\n\'\n'
        "    'chmod +x \"$UV_UNMANAGED_INSTALL/uv\"\\n')\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    result = subprocess.run(
        ["bash", str(project / "start.sh"), "--help"],
        env=environment,
        input="",
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "Downloading uv 0.12.23" in result.stderr
    request = json.loads(bootstrap_log.read_text())
    assert request[-1] == "https://astral.sh/uv/0.12.23/install.sh"
    assert request[:2] == ["--proto", "=https"]
    assert (project / ".potbot/runtime/uv/uv").is_file()
    assert len(log.read_text().splitlines()) == 3
