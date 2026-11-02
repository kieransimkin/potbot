"""Operator deployment boundaries; mocked host setup is not actual custody evidence."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.syn_release import host
from potbot.syn_release.contracts import Policy, canonical
from potbot.syn_release.operator import SynOperatorDeployment
from potbot.syn_release.store import native_platform
from tests.unit.test_syn_release_installation import make_bundle


def operator_policy():
    bundle = make_bundle("0.2.0")
    value = {
        **bundle.value,
        "component": "runtime",
        "mode": "operator",
        "required_profiles": ["syn.operator-local/1"],
        "required_capabilities": [
            "authenticated-operator-ipc",
            "native-os-operator-custody",
            "retained-operator-identity",
        ],
    }
    return Policy.parse(canonical(value))


@pytest.mark.parametrize(
    "field,value",
    [
        ("component", "client"),
        ("mode", "service"),
        ("required_profiles", []),
        ("required_capabilities", []),
    ],
)
def test_operator_requirements_refuse_before_touching_any_host(field, value, tmp_path):
    policy = operator_policy()
    policy.value[field] = value
    destination = tmp_path / "must-not-create"
    with pytest.raises(ScopeDeniedError, match="explicit upstream operator"):
        SynOperatorDeployment(SimpleNamespace(root=tmp_path / "packages"), policy, destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    "system,machine,expected",
    [
        ("Linux", "x86_64", "linux-x86_64"),
        ("Darwin", "arm64", "darwin-arm64"),
        ("Darwin", "x86_64", "darwin-x86_64"),
    ],
)
def test_native_platform_profile_is_explicit(monkeypatch, system, machine, expected):
    if os.name != "posix":
        pytest.skip("Native POSIX installer profile; Windows uses the tested WSL plan")
    from potbot.syn_release import store

    monkeypatch.setattr(store.platform, "system", lambda: system)
    monkeypatch.setattr(store.platform, "machine", lambda: machine)
    assert native_platform() == expected


@pytest.fixture
def managed_host(monkeypatch, tmp_path):
    monkeypatch.setattr(host, "is_windows", lambda: True)
    monkeypatch.setenv("SystemRoot", str(tmp_path / "windows"))
    current = set()
    calls = []

    def run(argv, *, timeout=30):
        calls.append((argv, timeout))
        if "--list" in argv:
            return "\n".join(sorted(current))
        if "--install" in argv:
            current.add(host.DISTRO)
        return "syn-native-prerequisites-ready\n"

    monkeypatch.setattr(host, "run_host", run)
    return host.WslOperatorHost(tmp_path / "managed"), current, calls


def test_windows_setup_installs_owned_wsl_and_native_service_without_claiming_keys(managed_host):
    manager, current, calls = managed_host
    record = manager.setup()
    (install,) = [argv for argv, _ in calls if "--install" in argv]
    assert install[install.index("--distribution") + 1] == "Ubuntu-24.04"
    assert install[install.index("--name") + 1] == host.DISTRO
    assert "--no-launch" in install and "--web-download" in install
    (provision,) = [argv for argv, _ in calls if "--exec" in argv and "/bin/sh" in argv]
    assert provision[-1] == host.PROVISION_UBUNTU
    assert "gnome-keyring" in provision[-1]
    assert any(
        argv[-4:] == ["/usr/bin/systemctl", "--user", "start", "gnome-keyring-daemon.service"]
        for argv, _ in calls
    )
    assert record["state"] == "ready"
    assert record["operator_identity_created"] is False
    assert record["keystore_unlocked"] is False
    manager.setup()
    assert len([argv for argv, _ in calls if "--install" in argv]) == 1


def test_unknown_existing_distribution_is_never_adopted_or_reinstalled(managed_host):
    manager, current, calls = managed_host
    current.add(host.DISTRO)
    with pytest.raises(StateMismatchError, match="no ownership receipt"):
        manager.setup()
    assert not any("--install" in argv or "--exec" in argv for argv, _ in calls)


def test_missing_registered_distribution_cannot_create_replacement(managed_host):
    manager, current, calls = managed_host
    manager.setup()
    current.clear()
    calls.clear()
    with pytest.raises(StateMismatchError, match="no silent replacement"):
        manager.setup()
    assert not any("--install" in argv for argv, _ in calls)


def test_corrupt_host_owner_and_lifecycle_fail_closed(managed_host):
    manager, _, _ = managed_host
    record = manager.setup()
    manager.receipt.write_text(json.dumps({**record, "state": "unknown"}))
    with pytest.raises(StateMismatchError, match="lifecycle/owner"):
        manager.setup()


def test_wsl_dispatch_preserves_arguments_without_a_shell(managed_host):
    manager, _, _ = managed_host
    manager.setup()
    argument = "value; $(do-not-run)"
    command = manager.command("/usr/bin/python3", ["-m", "potbot", argument])
    assert command[-4:] == ["/usr/bin/python3", "-m", "potbot", argument]
    assert command[command.index("--user") + 1] == host.OPERATOR_USER
    assert "/bin/sh" not in command


def test_wsl_utf16_console_output_is_decoded_without_nulls():
    assert host._decode("Ubuntu\n".encode("utf-16-le")) == "Ubuntu\n"


def test_mac_native_profile_does_not_install_another_vm(monkeypatch):
    monkeypatch.setattr(host.platform, "system", lambda: "Darwin")
    assert host.native_host_setup()["custody"] == "macos-keychain"


def test_systemd_arguments_preserve_dollar_and_specifier_literals():
    from potbot.syn_release.system_service import systemd_argument

    assert systemd_argument("/path/$VALUE/%n") == '"/path/$$VALUE/%%n"'
    with pytest.raises(ScopeDeniedError, match="control"):
        systemd_argument("/path/\ncommand")


@pytest.fixture
def native_deployment(tmp_path):
    if os.name != "posix":
        pytest.skip("Native POSIX operator boundary")
    return SynOperatorDeployment(
        SimpleNamespace(root=tmp_path / "packages"), operator_policy(), tmp_path / "node"
    )


def test_operator_environment_drops_tokens_and_python_overrides(native_deployment, monkeypatch):
    monkeypatch.setenv("POTBOT_SYN_GITHUB_TOKEN", "disposable-test-token")
    monkeypatch.setenv("PYTHONPATH", "/untrusted")
    monkeypatch.setenv("SYN_RELEASE_SIGNING_KEY_BASE64", "not-a-key")
    value = native_deployment._environment()
    assert "POTBOT_SYN_GITHUB_TOKEN" not in value
    assert "SYN_RELEASE_SIGNING_KEY_BASE64" not in value
    assert "PYTHONPATH" not in value
    assert value["HOME"].startswith("/")


def test_second_supervisor_cannot_claim_existing_controller(native_deployment):
    with native_deployment._supervisor_lock():
        with pytest.raises(StateMismatchError, match="Another supervisor"):
            with native_deployment._supervisor_lock():
                pytest.fail("Overlapping supervisor acquired private lifecycle")


def test_supervisor_fences_foreign_endpoint_and_stops_only_its_child(
    native_deployment, monkeypatch
):
    import subprocess
    import sys

    from potbot.syn_release import operator

    original_popen = subprocess.Popen
    children = []

    def launch(*args, **kwargs):
        child = original_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        children.append(child)
        return child

    record = {
        "installation": "fixture",
        "base_public_key_hash": "ab" * 32,
    }
    monkeypatch.setattr(
        native_deployment,
        "_installation",
        lambda: ({"directory": "fixture", "observed": {"runtime": {}}}, sys.executable),
    )
    monkeypatch.setattr(native_deployment, "_registered", lambda: record)
    monkeypatch.setattr(native_deployment, "_status", lambda *_: {"status": {"pid": os.getpid()}})
    monkeypatch.setattr(operator.subprocess, "Popen", launch)
    with pytest.raises(StateMismatchError, match="not the child"):
        native_deployment.supervise()
    assert len(children) == 1 and children[0].poll() is not None
    lifecycle = json.loads((native_deployment.root / "lifecycle.json").read_text())
    assert lifecycle["state"] == "fenced"
    assert lifecycle["agent_activation_authorized"] is False


def test_changed_policy_fences_health_before_invoking_upstream(native_deployment, tmp_path):
    changed = dict(native_deployment.policy.value)
    changed["minimum_version"] = "0.1.1"
    path = tmp_path / "policy.json"
    path.write_bytes(canonical(changed))
    path.chmod(0o600)
    native_deployment.policy_path = path
    with pytest.raises(StateMismatchError, match="trust policy changed"):
        native_deployment._status(None, {})


def test_unexpected_successful_child_exit_is_fenced(native_deployment, monkeypatch):
    import subprocess
    import sys

    from potbot.syn_release import operator

    original = subprocess.Popen

    def launch(*args, **kwargs):
        return original([sys.executable, "-c", "pass"], **kwargs)

    monkeypatch.setattr(operator.subprocess, "Popen", launch)
    monkeypatch.setattr(
        native_deployment,
        "_installation",
        lambda: ({"directory": "fixture", "observed": {"runtime": {}}}, sys.executable),
    )
    monkeypatch.setattr(
        native_deployment,
        "_registered",
        lambda: {"installation": "fixture", "base_public_key_hash": "ab" * 32},
    )

    def unavailable(*args):
        raise StateMismatchError("Disposable child has no operator endpoint")

    monkeypatch.setattr(native_deployment, "_status", unavailable)
    with pytest.raises(StateMismatchError, match="exited unexpectedly"):
        native_deployment.supervise()
    assert json.loads((native_deployment.root / "lifecycle.json").read_text())["state"] == "fenced"


def test_revoked_policy_refuses_health_before_invoking_upstream(native_deployment, tmp_path):
    changed = dict(native_deployment.policy.value)
    changed["revoked_manifests"] = [changed["manifest_sha256"]]
    path = tmp_path / "policy.json"
    path.write_bytes(canonical(changed))
    path.chmod(0o600)
    native_deployment.policy_path = path
    with pytest.raises(ScopeDeniedError, match="revoked"):
        native_deployment._status(None, {})


def test_wsl_controller_digest_is_checked_before_native_install(managed_host, tmp_path):
    from potbot.syn_release.wsl_controller import install_controller

    manager, _, calls = managed_host
    wheel = tmp_path / "potbot-0.5.0.dev9-py3-none-any.whl"
    wheel.write_bytes(b"synthetic-not-a-controller")
    with pytest.raises(StateMismatchError, match="differs"):
        install_controller(manager, wheel, "00" * 32)
    assert calls == []


def test_wsl_controller_cannot_be_redirected_to_another_executable(managed_host):
    from potbot.syn_release.wsl_controller import forward

    manager, _, calls = managed_host
    record = manager.setup()
    record["controller"] = {
        "profile": "potbot.syn-wsl-controller/1",
        "wheel_sha256": "ab" * 32,
        "python": "/tmp/untrusted-python",
    }
    manager.receipt.write_text(json.dumps(record))
    calls.clear()
    with pytest.raises(StateMismatchError, match="exact reviewed"):
        forward(manager, "syn-operator", ["attach"])
    assert calls == []


def test_windows_lifetime_helpers_parse_in_the_native_powershell_host(tmp_path):
    if os.name != "nt":
        pytest.skip("Actual Windows PowerShell parser qualification")
    import subprocess
    from pathlib import Path

    from potbot.syn_release.windows_service import INSTALL, WAIT

    parser = tmp_path / "parse.ps1"
    parser.write_text(
        "param([string]$Source)\n$tokens=$null;$errors=$null\n"
        "[System.Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors) | Out-Null\n"
        "if($errors.Count){exit 2}\n",
        encoding="utf-8",
    )
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    for index, source in enumerate((INSTALL, WAIT)):
        path = tmp_path / f"native-{index}.ps1"
        path.write_text(source, encoding="utf-8")
        result = subprocess.run(
            [
                str(executable),
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(parser),
                "-Source",
                str(path),
            ],
            capture_output=True,
            timeout=20,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert result.returncode == 0, "Native Windows task helper did not parse"
