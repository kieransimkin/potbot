"""The sandbox smoke cannot pass when denial commands were never dispatched."""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
import shlex
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest


def _qualifier():
    source = Path(__file__).resolve().parents[2] / "scripts/qualify_shared_workspace.py"
    spec = importlib.util.spec_from_file_location("potbot_shared_qualification", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _result(
    *,
    outcome: str,
    exit_code: int | None,
    dispatched: bool = True,
    stopped: bool = True,
    identity: str | None = "a" * 64,
) -> SimpleNamespace:
    return SimpleNamespace(
        outcome=outcome,
        exit_code=exit_code,
        stdout=b"",
        stderr=b"",
        dispatched=dispatched,
        processes_stopped=stopped,
        isolation_identity=identity,
        stdout_truncated=False,
        stderr_truncated=False,
    )


@pytest.mark.parametrize(
    "case,must_succeed,expected",
    [
        (_result(outcome="completed", exit_code=0), True, True),
        (_result(outcome="nonzero_exit", exit_code=1), False, True),
        (_result(outcome="nonzero_exit", exit_code=1, dispatched=False), False, False),
        (_result(outcome="nonzero_exit", exit_code=1, stopped=False), False, False),
        (_result(outcome="nonzero_exit", exit_code=1, identity=None), False, False),
        (_result(outcome="infrastructure_error", exit_code=None), False, False),
        (_result(outcome="timeout", exit_code=None), False, False),
        (_result(outcome="completed", exit_code=0), False, False),
        (_result(outcome="nonzero_exit", exit_code=5), True, False),
        (_result(outcome="completed", exit_code=0, dispatched=False), True, False),
    ],
)
def test_negative_case_is_never_confused_with_failed_sandbox_launch(
    tmp_path: Path,
    case: SimpleNamespace,
    must_succeed: bool,
    expected: bool,
    monkeypatch,
) -> None:
    class FakeSupervisor:
        def run(self, request: object) -> SimpleNamespace:
            return case

    module = _qualifier()
    monkeypatch.setattr(module, "verify_gui_run", _audit)
    result = module._case(
        FakeSupervisor(),
        home=tmp_path,
        command="false",
        must_succeed=must_succeed,
        workspace_id=uuid4(),
        principal_id=uuid4(),
        audit_root=tmp_path,
    )
    assert result["passed"] is expected
    assert result["dispatched"] is case.dispatched
    assert result["processes_stopped"] is case.processes_stopped


def test_launch_diagnostic_does_not_echo_paths_or_arbitrary_reasons():
    classify = _qualifier()._launch_diagnostic
    assert classify(b"bwrap: Can't mount proc on /private/root: Permission denied\n") == {
        "stage": "bubblewrap_mount",
        "reason": "permission_denied",
    }
    assert classify(b"bwrap: /private/path: internal data\n") == {
        "stage": "bubblewrap_other",
        "reason": "other",
    }
    assert classify(b"unrecognized output") is None


@pytest.mark.skipif(os.name != "posix", reason="Linux/WSL /proc namespace identifiers")
def test_smoke_checks_actual_kernel_network_namespace(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".tmp").mkdir(parents=True)
    staging = tmp_path / "staging"
    tools = tmp_path / "tools"
    staging.mkdir()
    tools.mkdir()
    commands: list[str] = []
    requests = []
    workspace, principal = uuid4(), uuid4()

    class FakeSupervisor:
        def run(self, request: object) -> SimpleNamespace:
            command = request.argv[-1]
            commands.append(command)
            requests.append(request)
            negative = command.startswith("touch /opt/potbot/models/") or (
                command.startswith("touch /etc/")
                or command.startswith("touch /usr/local/tools/")
                or command.startswith("/usr/bin/python3 -I -S -c ")
            )
            return _result(
                outcome="nonzero_exit" if negative else "completed", exit_code=1 if negative else 0
            )

    # Supply the fake supervisor by replacing the constructor, retaining the
    # production smoke test's actual case list and receipt logic.
    module = _qualifier()
    runtime = module.SharedWorkspaceRuntime(
        "a" * 64,
        tmp_path / "image",
        home,
        "b" * 64,
        model_staging=staging,
        shared_models=tmp_path / "models",
        shared_tools=tools,
    )
    config = SimpleNamespace(audit_root=tmp_path / "audit")
    monkeypatch.setattr(module.WorkerMonitorConfig, "from_environment", lambda: config)
    monkeypatch.setattr(module, "verify_gui_run", _audit)

    with (
        patch.object(module, "ProcessSupervisor", return_value=FakeSupervisor()),
        patch.object(module, "SharedWorkspaceBubblewrapBackend", return_value=object()),
    ):
        report = module.qualify(runtime, workspace_id=workspace, principal_id=principal)
    assert report["qualified_smoke"] is True
    assert report["tests"]["network_namespace_isolated"]["passed"] is True
    namespace_check = next(command for command in commands if "/proc/self/ns/net" in command)
    assert os.readlink("/proc/self/ns/net") in namespace_check
    assert len(commands) == 11

    assert len({r.telemetry_run_id for r in requests}) == 11
    assert all(isinstance(r.telemetry_run_id, UUID) for r in requests)
    assert all(r.telemetry_workspace_id == workspace for r in requests)
    assert all(r.telemetry_agent_id == principal for r in requests)
    assert all(r.telemetry_identity_kind == "principal" for r in requests)
    assert {str(r.telemetry_run_id) for r in requests} == {
        row["run_id"] for row in report["tests"].values()
    }
    assert all(row["audit_verified"] is True for row in report["tests"].values())
    assert report["workspace_id"] == str(workspace)
    assert report["principal_id"] == str(principal)
    assert report["schema"].endswith("/v2")
    receipt = report.pop("receipt_sha256")
    assert (
        receipt
        == hashlib.sha256(
            json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def _audit(root, workspace, run, *, identity_kind, subject_id):
    """Explicit unit-test double, never live kernel evidence."""
    assert identity_kind == "principal" and isinstance(subject_id, UUID)
    return {
        "qualified": True,
        "workspaceId": str(workspace),
        "runId": str(run),
        "identityKind": identity_kind,
    }


def _case_with(module, tmp_path, result, *, must_succeed=True):
    requests = []

    class FakeSupervisor:
        def run(self, request):
            requests.append(request)
            return result

    report = module._case(
        FakeSupervisor(),
        home=tmp_path,
        command="false",
        must_succeed=must_succeed,
        workspace_id=uuid4(),
        principal_id=uuid4(),
        audit_root=tmp_path,
    )
    return report, requests[0]


@pytest.mark.parametrize(
    "field,value",
    [
        ("dispatched", 1),
        ("dispatched", "true"),
        ("processes_stopped", 1),
        ("processes_stopped", "true"),
        ("stdout_truncated", True),
        ("stderr_truncated", True),
        ("stdout_truncated", None),
        ("exit_code", False),
        ("exit_code", 0.0),
        ("isolation_identity", "not-an-isolation-digest"),
        ("isolation_identity", "A" * 64),
        ("isolation_identity", ""),
        ("isolation_identity", True),
    ],
)
def test_smoke_rejects_truthy_or_incomplete_process_evidence(tmp_path, monkeypatch, field, value):
    module = _qualifier()
    result = _result(outcome="completed", exit_code=0)
    setattr(result, field, value)
    with patch.object(module, "verify_gui_run", side_effect=AssertionError("must not read audit")):
        report, _ = _case_with(module, tmp_path, result)
    assert report["passed"] is False
    assert report["audit_verified"] is False


@pytest.mark.parametrize("exit_code", [True, 1.0, -9, 5, 126, 127, None])
def test_shell_failure_and_signal_are_not_isolation_denial(tmp_path, monkeypatch, exit_code):
    module = _qualifier()
    monkeypatch.setattr(module, "verify_gui_run", _audit)
    report, _ = _case_with(
        module, tmp_path, _result(outcome="nonzero_exit", exit_code=exit_code), must_succeed=False
    )
    assert report["passed"] is False


@pytest.mark.parametrize("must_succeed", [False, True])
@pytest.mark.parametrize(
    "problem", ["missing", "damaged", "wrong-run", "wrong-workspace", "wrong-kind", "truthy"]
)
def test_complete_scoped_audit_is_mandatory(tmp_path, monkeypatch, must_succeed, problem):
    module = _qualifier()
    selected = []

    def evidence(root, workspace, run, **kwargs):
        selected.append((root, workspace, run, kwargs))
        if problem == "missing":
            raise FileNotFoundError("private-audit-path")
        if problem == "damaged":
            raise ValueError("private-transcript-content")
        doc = _audit(root, workspace, run, **kwargs)
        if problem == "wrong-run":
            doc["runId"] = str(uuid4())
        elif problem == "wrong-workspace":
            doc["workspaceId"] = str(uuid4())
        elif problem == "wrong-kind":
            doc["identityKind"] = "agent"
        else:
            doc["qualified"] = 1
        return doc

    monkeypatch.setattr(module, "verify_gui_run", evidence)
    result = _result(
        outcome="completed" if must_succeed else "nonzero_exit", exit_code=0 if must_succeed else 1
    )
    report, request = _case_with(module, tmp_path, result, must_succeed=must_succeed)
    assert report["passed"] is False and report["audit_verified"] is False
    assert len(selected) == 1
    assert selected[0][1:3] == (request.telemetry_workspace_id, request.telemetry_run_id)
    assert selected[0][3] == {
        "identity_kind": "principal",
        "subject_id": request.telemetry_agent_id,
    }
    assert "private-" not in json.dumps(report)


@pytest.mark.parametrize(
    "workspace,principal", [(None, uuid4()), (uuid4(), None), ("x", uuid4()), (uuid4(), "x")]
)
def test_smoke_identity_rejected_before_dispatch(tmp_path, workspace, principal):
    module = _qualifier()
    with pytest.raises(ValueError, match="explicit operator"):
        module._case(
            object(),
            home=tmp_path,
            command="false",
            must_succeed=False,
            workspace_id=workspace,
            principal_id=principal,
            audit_root=tmp_path,
        )


@pytest.mark.skipif(not Path("/bin/bash").is_file(), reason="fixed Bash probe semantics")
@pytest.mark.parametrize(
    "observed,status,expected",
    [
        ("net:[101]", 0, False),
        ("net:[102]", 0, True),
        ("", 0, False),
        ("net:[102]", 1, False),
        ("", 1, False),
        ("junk", 0, False),
        ("net:[]", 0, False),
        ("net:[102]suffix", 0, False),
        ("net:[102]\nnet:[103]", 0, False),
    ],
)
def test_namespace_probe_requires_successful_nonempty_well_formed_readlink(
    observed, status, expected
):
    # A trusted fixed shell expression is tested, not an actual sandbox launch.
    module = _qualifier()
    stub = f"readlink() {{ printf '%s' {shlex.quote(observed)}; return {status}; }}; "
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            stub + module._network_namespace_command("net:[101]"),
        ],
        capture_output=True,
        timeout=5,
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert (result.returncode == 0) is expected


@pytest.mark.parametrize("namespace", ["", "net:[]", "net:[1]; true", "net:[1]\n", None])
def test_invalid_host_namespace_is_not_embedded_in_shell(namespace):
    with pytest.raises(ValueError, match="host network namespace"):
        _qualifier()._network_namespace_command(namespace)


@pytest.fixture
def runtime(tmp_path):
    module = _qualifier()
    paths = {
        name: tmp_path / name
        for name in ("image", "home", "staging", "models", "tools", "skills", "config", "history")
    }
    for path in paths.values():
        path.mkdir(mode=0o700)
    (paths["home"] / ".tmp").mkdir(mode=0o700)
    return module.SharedWorkspaceRuntime(
        "a" * 64,
        paths["image"],
        paths["home"],
        "b" * 64,
        model_staging=paths["staging"],
        shared_models=paths["models"],
        shared_tools=paths["tools"],
        shared_skills=paths["skills"],
    )


def test_no_monitor_configuration_is_a_hard_failure_before_probe(runtime, monkeypatch):
    module = _qualifier()

    def unavailable():
        raise RuntimeError("kernel monitoring is not configured")

    monkeypatch.setattr(module.WorkerMonitorConfig, "from_environment", unavailable)
    with patch.object(module, "ProcessSupervisor") as supervisor:
        with pytest.raises(RuntimeError, match="kernel monitoring"):
            module.qualify(runtime, workspace_id=uuid4(), principal_id=uuid4())
        supervisor.assert_not_called()


def test_unconfirmed_shutdown_stops_the_suite_and_preserves_probe_files(runtime, monkeypatch):
    module = _qualifier()
    requests = []

    class UnconfirmedSupervisor:
        def run(self, request):
            requests.append(request)
            return _result(outcome="inconclusive", exit_code=None, stopped=False)

    monkeypatch.setattr(module, "SharedWorkspaceBubblewrapBackend", lambda *a, **kw: object())
    monkeypatch.setattr(module, "ProcessSupervisor", lambda *a, **kw: UnconfirmedSupervisor())
    with patch.object(module, "_remove_marker") as remove:
        with pytest.raises(RuntimeError, match="operator reconciliation"):
            module.qualify(
                runtime,
                workspace_id=uuid4(),
                principal_id=uuid4(),
                monitor_config=SimpleNamespace(audit_root=runtime.home.parent / "audit"),
            )
        remove.assert_not_called()
    assert len(requests) == 1


@pytest.mark.parametrize(
    "field", ["rootfs", "home", "model_staging", "shared_models", "shared_tools", "shared_skills"]
)
def test_report_cannot_be_written_inside_any_guest_mount(runtime, field):
    with pytest.raises(ValueError, match="every guest mount"):
        _qualifier()._report_destination(getattr(runtime, field) / "report.json", runtime)


def test_report_requires_fresh_private_real_parent(runtime, tmp_path):
    module = _qualifier()
    private = tmp_path / "operator"
    private.mkdir(mode=0o700)
    report = private / "report.json"
    assert module._report_destination(report, runtime) == report
    module.write_report(report, {"qualified_smoke": False})
    saved = report.read_bytes()
    with pytest.raises(ValueError, match="fresh canonical"):
        module._report_destination(report, runtime)
    with pytest.raises(FileExistsError):
        module.write_report(report, {"qualified_smoke": True})
    assert report.read_bytes() == saved
    assert report.stat().st_mode & 0o077 == 0
    private.chmod(0o755)
    with pytest.raises(ValueError, match="operator-private"):
        module._report_destination(private / "second.json", runtime)


def test_report_parent_and_marker_cleanup_refuse_symlink_redirection(runtime, tmp_path):
    module = _qualifier()
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    (target / "marker").write_bytes(b"keep")
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="canonical"):
        module._report_destination(alias / "report.json", runtime)
    with pytest.raises(OSError):
        module._remove_marker(alias, "marker")
    assert (target / "marker").read_bytes() == b"keep"
    (runtime.home / ".tmp" / "marker").symlink_to(target / "marker")
    module._remove_marker(runtime.home / ".tmp", "marker")
    assert (target / "marker").read_bytes() == b"keep"
    assert not (runtime.home / ".tmp" / "marker").is_symlink()


def test_cli_rejects_guest_report_before_constructing_a_supervisor(runtime, monkeypatch):
    module = _qualifier()
    args = [
        "--rootfs",
        str(runtime.rootfs),
        "--home",
        str(runtime.home),
        "--model-staging",
        str(runtime.model_staging),
        "--shared-models",
        str(runtime.shared_models),
        "--shared-tools",
        str(runtime.shared_tools),
        "--image-digest",
        runtime.image_digest,
        "--workspace-id",
        str(uuid4()),
        "--principal-id",
        str(uuid4()),
        "--report",
        str(runtime.shared_tools / "report.json"),
    ]
    with patch.object(module, "qualify") as qualify:
        assert module.main(args) == 2
        qualify.assert_not_called()


@pytest.mark.parametrize(
    "errors,expected",
    [
        ([errno.EPERM, errno.EPERM], 1),
        ([errno.EACCES, errno.EPERM], 1),
        ([None], 0),
        ([errno.EPERM, None], 0),
        ([errno.EAFNOSUPPORT], 2),
        ([errno.ENETUNREACH], 2),
        ([errno.EPERM, errno.ETIMEDOUT], 2),
    ],
)
def test_socket_probe_requires_explicit_denial_without_any_connection(
    monkeypatch, errors, expected
):
    command = shlex.split(_qualifier()._network_denial_command())
    assert command[:4] == ["/usr/bin/python3", "-I", "-S", "-c"]
    assert len(command) == 5
    families, closed = [], []

    class UnconnectedSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)

        def connect(self, *args):
            raise AssertionError("smoke must never contact an external endpoint")

    def create(family, kind):
        assert kind == socket.SOCK_STREAM
        index = len(families)
        families.append(family)
        error = errors[index]
        if error is not None:
            raise OSError(error, "fixture socket outcome")
        return UnconnectedSocket()

    monkeypatch.setattr(socket, "socket", create)
    with pytest.raises(SystemExit) as stopped:
        exec(compile(command[4], "<fixed-sandbox-socket-probe>", "exec"), {})
    assert stopped.value.code == expected
    assert families == [socket.AF_INET, socket.AF_INET6][: len(errors)]
    assert len(closed) == errors.count(None)
