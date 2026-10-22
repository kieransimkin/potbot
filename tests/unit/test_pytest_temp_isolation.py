"""Regression checks for Potbot-wide pytest temporary-directory isolation."""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _project(tmp_path: Path) -> Path:
    """Build a tiny self-contained pytest project to exercise the real plugin."""
    project = tmp_path / "probe-project"
    project.mkdir()
    source = Path(__file__).resolve().parents[2] / "conftest.py"
    shutil.copyfile(source, project / "conftest.py")
    (project / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (project / "test_probe.py").write_text(
        "from pathlib import Path\n"
        "def test_tmp_path(tmp_path, request):\n"
        "    base = Path(request.config.option.basetemp).resolve()\n"
        "    assert tmp_path.resolve().is_relative_to(base)\n"
        "    print('POTBOT_BASETEMP=' + str(base))\n",
        encoding="utf-8",
    )
    return project


def _environment(temp_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in ("TMPDIR", "TMP", "TEMP"):
        env[name] = str(temp_root)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return env


def _base(output: str) -> Path:
    records = [line.split("POTBOT_BASETEMP=", 1)[1].strip()
               for line in output.splitlines() if "POTBOT_BASETEMP=" in line]
    assert len(records) == 1, output
    return Path(records[0])


def _call(project: Path, env: dict[str, str], *args: str) -> tuple[int, str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-s", *args, "test_probe.py"],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    return completed.returncode, completed.stdout + completed.stderr


def test_no_shared_pytest_of_user_directory(tmp_path: Path) -> None:
    project = _project(tmp_path)
    temp_root = tmp_path / "writable-temp"
    temp_root.mkdir()
    # If pytest used its default shared folder, this ordinary file would block
    # creation of the pytest-of-<username> directory on both Windows and Linux.
    (temp_root / ("pytest-of-" + getpass.getuser())).write_text("blocked", encoding="utf-8")
    env = _environment(temp_root)
    result, output = _call(project, env)
    assert result == 0, output
    base = _base(output)
    assert base.is_relative_to(temp_root)
    assert base.parent.name.startswith("potbot-pytest-")


def test_multiple_pytest_processes_use_different_bases(tmp_path: Path) -> None:
    project = _project(tmp_path)
    temp_root = tmp_path / "writable-temp"
    temp_root.mkdir()
    env = _environment(temp_root)
    first_result, first_output = _call(project, env)
    second_result, second_output = _call(project, env)
    assert first_result == 0, first_output
    assert second_result == 0, second_output
    assert _base(first_output) != _base(second_output)


def test_explicit_basetemp_is_not_overridden(tmp_path: Path) -> None:
    project = _project(tmp_path)
    temp_root = tmp_path / "writable-temp"
    temp_root.mkdir()
    explicit = tmp_path / "explicit base"
    result, output = _call(project, _environment(temp_root), f"--basetemp={explicit}")
    assert result == 0, output
    assert _base(output) == explicit.resolve()
