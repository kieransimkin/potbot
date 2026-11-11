"""Operator deployment boundaries; mocked host setup is not actual custody evidence."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.domain.errors import CapabilityUnavailableError, ScopeDeniedError, StateMismatchError
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
    manager = host.WslOperatorHost(tmp_path / "managed")
    registration = host.WslRegistration(uuid4().hex, manager.location, 2)

    def run(argv, *, timeout=30):
        calls.append((argv, timeout))
        if argv[1:] == ["--version"]:
            return "WSL version: 2.6.3.0\n"
        if "--install" in argv and "--no-distribution" not in argv:
            current.add(host.DISTRO)
        return "syn-native-prerequisites-ready\n"

    monkeypatch.setattr(host, "run_host", run)
    monkeypatch.setattr(
        host, "_registered_distro", lambda: registration if host.DISTRO in current else None
    )
    return manager, current, calls


def test_windows_setup_installs_owned_wsl_and_native_service_without_claiming_keys(managed_host):
    manager, current, calls = managed_host
    record = manager.setup()
    (install,) = [argv for argv, _ in calls if "--install" in argv]
    assert install[install.index("--distribution") + 1] == "Ubuntu-24.04"
    assert install[install.index("--name") + 1] == host.DISTRO
    assert install[install.index("--version") + 1] == "2"
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
    assert manager.setup() == record
    assert len([argv for argv, _ in calls if "--install" in argv]) == 1


def test_setup_resumes_after_wsl_defers_registration_for_reboot(managed_host, monkeypatch):
    manager, current, calls = managed_host
    run = host.run_host

    def defer_install(argv, *, timeout=30):
        output = run(argv, timeout=timeout)
        if "--install" in argv:
            current.clear()
        return output

    monkeypatch.setattr(host, "run_host", defer_install)
    with pytest.raises(CapabilityUnavailableError, match="restart, then rerun"):
        manager.setup()
    pending = manager._read_receipt()
    assert pending["state"] == "registration-pending"
    assert not any("--exec" in argv for argv, _ in calls)

    monkeypatch.setattr(host, "run_host", run)
    ready = manager.setup()
    assert ready["state"] == "ready" and ready["registration_id"]
    assert ready["installation_id"] == pending["installation_id"]


def test_setup_prepares_missing_wsl_platform_without_installing_default_ubuntu(
    managed_host, monkeypatch
):
    manager, _, calls = managed_host
    run = host.run_host

    def run_before_reboot(argv, *, timeout=30):
        if argv[1:] == ["--version"]:
            return "Older WSL command usage"
        return run(argv, timeout=timeout)

    monkeypatch.setattr(host, "run_host", run_before_reboot)
    with pytest.raises(CapabilityUnavailableError, match="platform setup ran"):
        manager.setup()
    (bootstrap,) = [argv for argv, _ in calls if "--install" in argv]
    assert bootstrap[1:] == ["--install", "--no-distribution", "--web-download"]
    pending = manager._read_receipt()
    assert pending["state"] == "registration-pending"

    monkeypatch.setattr(host, "run_host", run)
    assert manager.setup()["installation_id"] == pending["installation_id"]


def test_setup_resumes_same_registration_after_provisioning_interrupts(managed_host, monkeypatch):
    manager, _, calls = managed_host
    run = host.run_host

    def interrupt_provisioning(argv, *, timeout=30):
        if argv[-1] == host.PROVISION_UBUNTU:
            raise CapabilityUnavailableError("Interrupted apt setup")
        return run(argv, timeout=timeout)

    monkeypatch.setattr(host, "run_host", interrupt_provisioning)
    with pytest.raises(CapabilityUnavailableError, match="Interrupted"):
        manager.setup()
    retained = manager._read_receipt()
    assert retained["state"] == "installing" and retained["registration_id"]

    monkeypatch.setattr(host, "run_host", run)
    assert manager.setup()["registration_id"] == retained["registration_id"]
    assert len([argv for argv, _ in calls if "--install" in argv]) == 1


def test_ready_prerequisites_survive_an_interrupted_keystore_start(managed_host, monkeypatch):
    manager, _, calls = managed_host
    run = host.run_host

    def interrupt_keystore(argv, *, timeout=30):
        if argv[-1] == "gnome-keyring-daemon.service":
            raise CapabilityUnavailableError("Interrupted keystore startup")
        return run(argv, timeout=timeout)

    monkeypatch.setattr(host, "run_host", interrupt_keystore)
    with pytest.raises(CapabilityUnavailableError, match="Interrupted"):
        manager.setup()
    assert manager.status()["setup_phase"] == "native-keystore"
    checkpoint = manager._read_receipt()
    resumed = host.WslOperatorHost(manager.root)
    monkeypatch.setattr(host, "run_host", run)
    assert resumed.setup()["registration_id"] == checkpoint["registration_id"]
    assert len([argv for argv, _ in calls if argv[-1] == host.PROVISION_UBUNTU]) == 1


@pytest.mark.parametrize("mismatch", ["path", "identifier", "version", "state"])
def test_setup_refuses_an_unsafe_wsl_registration(managed_host, monkeypatch, mismatch):
    manager, _, calls = managed_host
    manager.setup()
    registered = host._registered_distro()
    assert registered is not None
    values = {
        "path": {"location": manager.root / "other"},
        "identifier": {"identifier": uuid4().hex},
        "version": {"version": 1},
        "state": {"state": 3},
    }
    changed = replace(registered, **values[mismatch])
    monkeypatch.setattr(host, "_registered_distro", lambda: changed)
    calls.clear()
    with pytest.raises((StateMismatchError, CapabilityUnavailableError)):
        manager.setup()
    assert not calls


def test_setup_refuses_unregistered_storage_on_retry(managed_host, monkeypatch):
    manager, _, calls = managed_host
    monkeypatch.setattr(host, "_registered_distro", lambda: None)
    manager.location.mkdir(parents=True)
    with pytest.raises(StateMismatchError, match="storage needs reconciliation"):
        manager.setup()
    assert not calls


def test_setup_lock_excludes_concurrent_setup_and_releases_on_exit(managed_host):
    manager, _, calls = managed_host
    manager.root.mkdir()
    with manager._setup_lock():
        with pytest.raises(StateMismatchError, match="in progress"):
            manager.setup()
    assert not calls
    assert manager.setup()["state"] == "ready"


def test_ready_legacy_receipt_binds_existing_registration_without_provisioning(managed_host):
    manager, _, calls = managed_host
    ready = manager.setup()
    old = {key: value for key, value in ready.items() if key != "registration_id"}
    manager.receipt.write_text(json.dumps(old), encoding="utf-8")
    calls.clear()

    assert manager.setup() == ready
    (keystore,) = calls
    assert keystore[0][-1] == "gnome-keyring-daemon.service"


def test_status_separates_host_readiness_from_worker_qualification(managed_host, monkeypatch):
    manager, _, calls = managed_host
    assert manager.status()["host_state"] == "absent"
    assert not calls and not manager.receipt.exists()
    manager.setup()
    calls.clear()
    probe = {
        "kernel": "stock-wsl",
        "accounting": "unsupported",
        "missing_options": ["CONFIG_TASKSTATS"],
    }

    def run(argv, *, timeout=30):
        calls.append(argv)
        return json.dumps(probe)

    monkeypatch.setattr(host, "run_host", run)
    status = manager.status()
    assert status["host_state"] == "ready"
    assert status["worker_prerequisites"] == probe
    assert status["worker_qualification"] == "required"
    (command,) = calls
    assert command[-5:] == ["/usr/bin/python3.12", "-I", "-B", "-c", host._ACCOUNTING_PROBE]


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


def test_wsl_controller_retries_same_pin_and_keeps_previous_selection_on_failure(
    managed_host, tmp_path, monkeypatch
):
    import hashlib

    from potbot.syn_release import wsl_controller

    manager, _, _ = managed_host
    wheel = tmp_path / "potbot-0.5.0.dev12-py3-none-any.whl"
    wheel.write_bytes(b"first reviewed wheel")
    first = hashlib.sha256(wheel.read_bytes()).hexdigest()
    native_calls = []

    def run(argv, *, timeout=30):
        if "/usr/bin/wslpath" in argv:
            return "/mnt/c/reviewed.whl\n"
        selected, _, disposition = argv[-3:]
        native_calls.append((selected, disposition))
        if selected != first:
            raise CapabilityUnavailableError("Interrupted controller installation")
        return json.dumps(wsl_controller._controller_receipt(first))

    monkeypatch.setattr(wsl_controller, "run_host", run)
    first_receipt = wsl_controller.install_controller(manager, wheel, first)
    assert wsl_controller.install_controller(manager, wheel, first) == first_receipt
    wheel.write_bytes(b"next reviewed wheel")
    second = hashlib.sha256(wheel.read_bytes()).hexdigest()
    with pytest.raises(CapabilityUnavailableError, match="Interrupted"):
        wsl_controller.install_controller(manager, wheel, second)
    assert manager._read_receipt()["controller"] == first_receipt
    assert manager._read_receipt()["pending_controller_sha256"] == second
    with pytest.raises(StateMismatchError, match="pending controller digest"):
        wheel.write_bytes(b"first reviewed wheel")
        wsl_controller.install_controller(manager, wheel, first)
    assert (
        wsl_controller.install_controller(manager, wheel, first, replace_pending=True)
        == first_receipt
    )
    assert "pending_controller_sha256" not in manager._read_receipt()
    assert native_calls == [
        (first, "new"),
        (first, "retained"),
        (second, "new"),
        (first, "retained"),
    ]


def test_windows_setup_resumes_the_pinned_controller_after_restart(
    managed_host, tmp_path, monkeypatch
):
    import hashlib

    from potbot.syn_release import wsl_controller

    manager, _, calls = managed_host
    ready_run = host.run_host
    wheel = tmp_path / "potbot-0.5.0.dev12-py3-none-any.whl"
    wheel.write_bytes(b"reviewed controller")
    pin = hashlib.sha256(wheel.read_bytes()).hexdigest()

    def before_restart(argv, *, timeout=30):
        if argv[1:] == ["--version"]:
            return "Older WSL command usage"
        if "--no-distribution" in argv:
            assert manager._read_receipt()["pending_controller_sha256"] == pin
            raise host.WslRestartRequired("Windows reported a restart is required")
        raise AssertionError("Unexpected WSL call before restart")

    monkeypatch.setattr(host, "run_host", before_restart)
    with pytest.raises(host.WslRestartRequired):
        wsl_controller.install_controller(manager, wheel, pin)
    interrupted = manager._read_receipt()
    assert manager.status() == {
        "profile": "potbot.syn-wsl-status/1",
        "host_state": "registration-pending",
        "worker_qualification": "required",
        "setup_phase": "windows-platform",
        "restart_required": True,
        "pending_controller_sha256": pin,
    }

    resumed = host.WslOperatorHost(manager.root)
    alternate = tmp_path / "other.whl"
    alternate.write_bytes(b"different reviewed controller")
    different_pin = hashlib.sha256(alternate.read_bytes()).hexdigest()
    monkeypatch.setattr(host, "run_host", ready_run)
    with pytest.raises(StateMismatchError, match="pending controller digest"):
        wsl_controller.install_controller(resumed, alternate, different_pin)
    assert not calls

    def install_native(argv, *, timeout=30):
        if "/usr/bin/wslpath" in argv:
            return "/mnt/c/reviewed.whl\n"
        assert argv[-3] == pin and argv[-1] == "new"
        return json.dumps(wsl_controller._controller_receipt(pin))

    monkeypatch.setattr(wsl_controller, "run_host", install_native)
    receipt = wsl_controller.install_controller(resumed, wheel, pin)
    after = resumed._read_receipt()
    assert after["controller"] == receipt
    assert after["installation_id"] == interrupted["installation_id"]
    assert after["state"] == "ready"
    assert not {"restart_required", "setup_phase", "pending_controller_sha256"} & after.keys()
    assert len([argv for argv, _ in calls if "--install" in argv]) == 1


@pytest.mark.parametrize("code", [3010, 1641, 0x80070BC2, -2147021886])
def test_windows_restart_codes_are_not_parsed_from_console_text(monkeypatch, code):
    from subprocess import CompletedProcess

    monkeypatch.setattr(host, "windows_environment", lambda: {})
    monkeypatch.setattr(
        host.subprocess, "run", lambda argv, **kwargs: CompletedProcess(argv, code, b"", b"")
    )
    with pytest.raises(host.WslRestartRequired):
        host.run_host(["wsl.exe", "--install", "--no-distribution"])
    with pytest.raises(CapabilityUnavailableError) as generic:
        host.run_host(["wsl.exe", "--version"])
    assert not isinstance(generic.value, host.WslRestartRequired)


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
