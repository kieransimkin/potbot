"""SYN-only fixture for bounded Linux AF_UNIX socket paths."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


@pytest.fixture(name="tmp_path")
def _syn_short_socket_tmp_path():
    """Private, short-lived test directory; avoids host-dependent pytest roots."""
    with TemporaryDirectory(prefix="syn-node-", dir="/tmp" if os.path.isdir("/tmp") else None) as name:
        yield Path(name)
