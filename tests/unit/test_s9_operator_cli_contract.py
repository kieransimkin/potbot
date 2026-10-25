"""The tool-upgrade maintenance CLI only reads exact operator-owned JSON."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from scripts.tool_upgrade_s9 import _plan, _uuid


def document() -> dict[str, object]:
    return {
        "schema": "potbot-shared-tool-upgrade-plan/v1",
        "expected_old_commit": "0" * 40,
        "replacement": {
            "slug": "stemlab",
            "repository": "https://github.com/kieransimkin/stemlab",
            "commit": "1" * 40,
            "cleanup_paths": ["build", ".pytest_cache"],
            "lockfiles": [],
            "model_downloads": False,
            "reusable_artifacts": [],
        },
        "tests": [
            {"name": "smoke-test", "kind": "smoke", "argv": ["/bin/bash", "-c", "true"],
             "timeout_ms": 2000},
            {"name": "unit-test", "kind": "unit", "argv": ["/usr/bin/git", "--version"],
             "timeout_ms": 2000},
        ],
    }


def fixture_plan(tmp_path: Path) -> Path:
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(document()))
    plan.chmod(0o600)
    return plan


def test_cli_plan_parses_version_pinned_suite(tmp_path: Path) -> None:
    path = fixture_plan(tmp_path)
    candidate, suite, prior = _plan(path)
    assert candidate.commit == "1" * 40
    assert prior == "0" * 40
    assert {case.kind for case in suite.cases} == {"smoke", "unit"}


def test_cli_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = fixture_plan(tmp_path)
    path.write_text(path.read_text().replace('"schema":', '"schema":"duplicate", "schema":'))
    with pytest.raises(InvalidInputError):
        _plan(path)


def test_cli_rejects_bad_owner_mode_and_symlink(tmp_path: Path) -> None:
    path = fixture_plan(tmp_path)
    path.chmod(0o666)
    with pytest.raises(ScopeDeniedError):
        _plan(path)
    path.chmod(0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(ScopeDeniedError):
        _plan(alias)


def test_cli_only_accepts_canonical_uuids() -> None:
    with pytest.raises(InvalidInputError):
        _uuid("../bad")
