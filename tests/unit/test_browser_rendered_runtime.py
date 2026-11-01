"""Test-only binary selection leaves the production launch interface unchanged."""

from __future__ import annotations

import asyncio
import hashlib
import os
from types import SimpleNamespace

import pytest

from scripts import browser_rendered_runtime as runtime


def executable(tmp_path):
    path = tmp_path / "engine-chromium"
    path.write_bytes(b"synthetic unit-test executable; never launched")
    path.chmod(0o755)
    return path


def browser_type(tmp_path):
    path = executable(tmp_path)

    class BrowserType:
        name = "chromium"
        executable_path = str(path)

        async def launch(self, **kwargs):
            return kwargs

    return BrowserType


def test_system_mode_does_not_import_or_patch_an_engine(monkeypatch):
    monkeypatch.delenv("POTBOT_BROWSER_TEST_BINARY", raising=False)
    monkeypatch.setattr(
        runtime.importlib, "import_module", lambda _: pytest.fail("unexpected import")
    )
    assert runtime.selection_mode() == "system"
    with runtime.selected_browser("playwright"):
        pass


@pytest.mark.parametrize("value", ["", "../browser", "custom", "/usr/bin/chromium"])
def test_arbitrary_binary_selection_is_not_accepted(monkeypatch, value):
    monkeypatch.setenv("POTBOT_BROWSER_TEST_BINARY", value)
    with pytest.raises(ValueError, match="selection"):
        runtime.selection_mode()


def test_engine_mode_requires_explicit_acceptance_opt_in(monkeypatch):
    monkeypatch.setenv("POTBOT_BROWSER_TEST_BINARY", "engine")
    monkeypatch.delenv("POTBOT_RUN_BROWSER_RENDERED", raising=False)
    with pytest.raises(ValueError, match="explicit rendered"):
        with runtime.selected_browser("playwright"):
            pytest.fail("unguarded engine selection")


@pytest.mark.parametrize("engine", ["unknown", "firefox", "../../module"])
def test_unknown_engine_never_imported(monkeypatch, engine):
    monkeypatch.setattr(
        runtime.importlib, "import_module", lambda _: pytest.fail("unexpected import")
    )
    with pytest.raises(ValueError, match="engine"):
        with runtime.selected_browser(engine):
            pass


def test_adapter_changes_only_the_fixed_executable_and_restores_method(tmp_path):
    cls = browser_type(tmp_path)
    original = cls.launch
    options = dict(
        executable_path="/usr/bin/chromium",
        headless=False,
        timeout=15000,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    with runtime._redirect_launch(cls):
        result = asyncio.run(cls().launch(**options))
        assert result == {**options, "executable_path": cls.executable_path}
    assert cls.launch is original
    assert options["executable_path"] == "/usr/bin/chromium"


def test_method_restored_after_test_failure(tmp_path):
    cls = browser_type(tmp_path)
    original = cls.launch
    with pytest.raises(RuntimeError):
        with runtime._redirect_launch(cls):
            raise RuntimeError("synthetic test failure")
    assert cls.launch is original


@pytest.mark.parametrize("path", [None, "/tmp/arbitrary", "chromium"])
def test_adapter_rejects_changed_production_launch_contract(tmp_path, path):
    cls = browser_type(tmp_path)
    with runtime._redirect_launch(cls), pytest.raises(ValueError, match="contract"):
        asyncio.run(cls().launch(executable_path=path))


def test_adapter_rejects_a_different_browser_type(tmp_path):
    cls = browser_type(tmp_path)
    cls.name = "firefox"
    with runtime._redirect_launch(cls), pytest.raises(ValueError, match="contract"):
        asyncio.run(cls().launch(executable_path="/usr/bin/chromium"))


def test_engine_adapter_uses_only_library_selected_binary(tmp_path, monkeypatch):
    cls = browser_type(tmp_path)
    imported = []
    monkeypatch.setenv("POTBOT_BROWSER_TEST_BINARY", "engine")
    monkeypatch.setenv("POTBOT_RUN_BROWSER_RENDERED", "1")

    def load(name):
        imported.append(name)
        return SimpleNamespace(BrowserType=cls)

    monkeypatch.setattr(runtime.importlib, "import_module", load)
    with runtime.selected_browser("patchright"):
        result = asyncio.run(cls().launch(executable_path="/usr/bin/chromium"))
    assert imported == ["patchright.async_api"]
    assert result["executable_path"] == cls.executable_path


def test_binary_identity_hashes_the_executable_without_exposing_its_host_path(tmp_path):
    path = executable(tmp_path)
    result = runtime.binary_identity(path, "engine")
    assert result == {
        "source": "engine",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
    }
    assert str(tmp_path) not in str(result)


@pytest.mark.parametrize("source", ["unknown", "", "relative"])
def test_binary_identity_requires_known_source(tmp_path, source):
    with pytest.raises(ValueError, match="source"):
        runtime.binary_identity(executable(tmp_path), source)


def test_binary_identity_rejects_missing_relative_and_nonexecutable_files(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        runtime.binary_identity("relative", "system")
    with pytest.raises(FileNotFoundError):
        runtime.binary_identity(tmp_path / "missing", "system")
    if os.name != "posix":
        return  # POSIX execute bits are not a portable Windows ACL test.
    path = executable(tmp_path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="unavailable"):
        runtime.binary_identity(path, "system")
