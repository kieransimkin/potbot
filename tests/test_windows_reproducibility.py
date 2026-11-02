"""Regression coverage for the cross-platform pytest scratch contract."""

from __future__ import annotations

import os
from pathlib import Path


def test_pytest_temp_root_supports_atomic_io(tmp_path: Path) -> None:
    marker = tmp_path / "marker.txt"
    renamed = tmp_path / "marker-renamed.txt"
    marker.write_text("pytest-temp-probe", encoding="utf-8")
    assert marker.read_text(encoding="utf-8") == "pytest-temp-probe"
    marker.rename(renamed)
    assert renamed.read_text(encoding="utf-8") == "pytest-temp-probe"
    renamed.unlink()
    assert not renamed.exists()

    if os.name == "nt":
        local_app_data = Path(os.environ["LOCALAPPDATA"]).resolve()
        assert local_app_data == tmp_path.resolve() or local_app_data in tmp_path.resolve().parents
