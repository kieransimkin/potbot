"""Give every Potbot pytest process a private temporary root.

Pytest's default ``pytest-of-<username>`` directory can retain broken ownership
or Windows locks across runs. Avoid that shared path entirely. This hook runs
before pytest creates its tmp_path factory, including when scripts/check.py
invokes pytest. A caller-supplied --basetemp always takes precedence.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Select an isolated, automatically cleaned base for this pytest process."""
    if config.option.basetemp is not None:
        return

    # Each TemporaryDirectory is created with a fresh, process-private name.
    # Use a child path so pytest never removes the directory owned by tempfile.
    # Windows may briefly lock a file on cleanup; don't fail the entire test run
    # merely because a temporary file cannot yet be deleted.
    owner = TemporaryDirectory(prefix="potbot-pytest-", ignore_cleanup_errors=True)
    config.option.basetemp = str(Path(owner.name) / "run")
    config.add_cleanup(owner.cleanup)
