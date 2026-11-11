"""Disposable application database fixture."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from potbot.persistence.harness import HarnessError
from scripts.postgres_qualification_fixture import AppEnvironment as AppEnvironment
from scripts.postgres_qualification_fixture import provisioned_app_env


@pytest.fixture
def app_env(tmp_path: Path) -> Iterator[AppEnvironment]:
    try:
        with provisioned_app_env(tmp_path) as environment:
            yield environment
    except HarnessError as error:
        pytest.fail(f"PostgreSQL application fixture unavailable: {error}", pytrace=False)
