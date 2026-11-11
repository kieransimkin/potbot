import codecs
from contextlib import nullcontext
from pathlib import PureWindowsPath
from types import SimpleNamespace

import pytest

from potbot import wsl_kernel_activation as activation
from potbot.cli import main
from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.wsl_kernel_activation import _Change, _configured, _read_local, _replace_local

BUNDLE = PureWindowsPath(r"C:\Potbot install\verified")
LINES = (
    rb"kernel=C:\\Potbot install\\verified\\bzImage",
    rb"kernelModules=C:\\Potbot install\\verified\\modules.vhdx",
)


def test_stage_keeps_existing_wsl_configuration_bytes():
    header = codecs.BOM_UTF8 + b"# comment\r\n[WSL2]\r\n"
    rest = b"memory=4GB\r\n\r\n[experimental]\r\nsparseVhd=true"
    assert _configured(header + rest, BUNDLE) == header + b"\r\n".join(LINES) + b"\r\n" + rest


@pytest.mark.parametrize(
    ("previous", "prefix", "newline"),
    [
        (None, b"[wsl2]\r\n", b"\r\n"),
        (b"[wsl2]", b"[wsl2]\n", b"\n"),
        (b"[experimental]\nsparseVhd=true", b"[experimental]\nsparseVhd=true\n[wsl2]\n", b"\n"),
    ],
)
def test_stage_creates_missing_wsl_section(previous, prefix, newline):
    assert _configured(previous, BUNDLE) == prefix + newline.join(LINES) + newline


@pytest.mark.parametrize(
    "previous",
    [
        b"[wsl2]\nKernel = C:\\external\n",
        b"[wsl2]\n[wsl2]\n",
        b"[wsl2",
    ],
)
def test_stage_refuses_unmanaged_or_ambiguous_configuration(previous):
    with pytest.raises(StateMismatchError):
        _configured(previous, BUNDLE)


def test_stage_rejects_nonlocal_paths():
    with pytest.raises(InvalidInputError, match="absolute Windows drive"):
        _configured(None, PureWindowsPath(r"\\server\share\bundle"))


@pytest.mark.parametrize("original", [None, b"", b"[wsl2]\r\nmemory=4GB\r\n"])
def test_wsl_change_retains_exact_baseline_and_refuses_a_different_current_config(
    tmp_path, original
):
    config = tmp_path / ".wslconfig"
    journal = tmp_path / "change.json"
    selected = _configured(original, BUNDLE)
    change = _Change(
        registration="1" * 32,
        original=original,
        staged_sha256="2" * 64,
        manifest_sha256="3" * 64,
        baseline_kernel="6.18.54-stock",
        baseline_boot="12345678-abcd-ef01-2345-6789abcdef01",
    )
    _replace_local(journal, before=None, after=change.encode())
    assert _Change.load(journal) == change
    if original is not None:
        config.write_bytes(original)
    _replace_local(config, before=original, after=selected)
    assert _read_local(config) == selected
    with pytest.raises(StateMismatchError, match="changed independently"):
        _replace_local(config, before=b"external", after=original)
    assert _read_local(config) == selected
    _replace_local(config, before=selected, after=original)
    assert _read_local(config) == original


@pytest.fixture
def simulated_activation(tmp_path, monkeypatch):
    host = SimpleNamespace(root=tmp_path, _setup_session=nullcontext)
    config = tmp_path / ".wslconfig"
    original = b"[wsl2]\r\nmemory=4GB\r\n"
    config.write_bytes(original)
    runner = activation.WslKernelActivation(host, config)
    monkeypatch.setattr(activation, "PureWindowsPath", lambda path: BUNDLE)
    monkeypatch.setattr(runner, "_registration", lambda: "1" * 32)
    monkeypatch.setattr(runner, "_require_idle", lambda: None)
    monkeypatch.setattr(runner, "_shutdown", lambda: None)
    candidate = "6.18.54-candidate"
    config_sha256 = "2" * 64
    manifest = {
        "kernel_release": candidate,
        "artifacts": {"kernel.config": {"sha256": config_sha256}},
    }
    monkeypatch.setattr(activation, "_stage_bundle", lambda *_args, **_kwargs: manifest)

    def boot(*, candidate=False):
        if candidate:
            assert config.read_bytes() == _configured(original, BUNDLE)
            assert runner.journal.is_file()
            return {
                "kernel_release": manifest["kernel_release"],
                "module_release": manifest["kernel_release"],
                "config_sha256": config_sha256,
                "boot_id": "candidate-boot",
            }
        assert config.read_bytes() == original
        return {"kernel_release": "6.18.54-stock", "boot_id": stock_boots.pop(0)}

    stock_boots = [
        "12345678-abcd-ef01-2345-6789abcdef01",
        "12345678-abcd-ef01-2345-6789abcdef02",
    ]
    monkeypatch.setattr(runner, "_boot", boot)
    return runner, original, boot


def test_verified_activation_can_be_rolled_back(simulated_activation):
    runner, original, _ = simulated_activation
    booted = runner.activate(BUNDLE, manifest_sha256="3" * 64, public_key="4" * 64)
    assert booted["activation"] == "boot_verified"
    assert booted["worker_qualification"] == "required"
    assert runner.config.read_bytes() == _configured(original, BUNDLE)
    assert runner.journal.is_file()

    restored = runner.rollback()
    assert restored["activation"] == "rolled_back"
    assert restored["kernel_release"] == "6.18.54-stock"
    assert runner.config.read_bytes() == original
    assert not runner.journal.exists()


def test_failed_boot_verification_restores_original_config(simulated_activation, monkeypatch):
    runner, original, boot = simulated_activation

    def bad_config(*, candidate=False):
        value = boot(candidate=candidate)
        return {**value, "config_sha256": "0" * 64} if candidate else value

    monkeypatch.setattr(runner, "_boot", bad_config)
    with pytest.raises(StateMismatchError, match="original configuration and boot were verified"):
        runner.activate(BUNDLE, manifest_sha256="3" * 64, public_key="4" * 64)
    assert runner.config.read_bytes() == original
    assert not runner.journal.exists()


def test_config_conflict_leaves_recovery_journal(simulated_activation, monkeypatch):
    runner, original, boot = simulated_activation
    external = original + b"# changed outside Potbot\r\n"

    def changed_config(*, candidate=False):
        value = boot(candidate=candidate)
        if candidate:
            runner.config.write_bytes(external)
        return value

    monkeypatch.setattr(runner, "_boot", changed_config)
    with pytest.raises(StateMismatchError, match="rollback needs attention"):
        runner.activate(BUNDLE, manifest_sha256="3" * 64, public_key="4" * 64)
    assert runner.config.read_bytes() == external
    assert _Change.load(runner.journal).original == original
    with pytest.raises(StateMismatchError, match="changed independently"):
        runner.rollback()
    assert runner.config.read_bytes() == external


def test_running_distributions_prevent_configuration_changes(simulated_activation, monkeypatch):
    runner, original, _ = simulated_activation
    monkeypatch.setattr(
        runner, "_require_idle", activation.WslKernelActivation._require_idle.__get__(runner)
    )
    monkeypatch.setattr(runner, "_running", lambda: {"another distro"})
    with pytest.raises(StateMismatchError, match="Close all WSL distributions"):
        runner.activate(BUNDLE, manifest_sha256="3" * 64, public_key="4" * 64)
    assert runner.config.read_bytes() == original
    assert not runner.journal.exists()


def test_installer_restart_requires_explicit_flag(capsys):
    with pytest.raises(SystemExit) as error:
        main(["wsl-kernel", "rollback"])
    assert error.value.code == 2
    assert "--restart-wsl" in capsys.readouterr().err
