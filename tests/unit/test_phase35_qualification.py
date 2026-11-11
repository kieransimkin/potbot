"""A fake exit receipt must never qualify the operator's live sandbox."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load():
    module_path = Path(__file__).resolve().parents[2] / "scripts/qualify_phase35_worker.py"
    spec = importlib.util.spec_from_file_location("phase35_qualification", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_qualification_requires_real_taskstats_and_forked_children():
    inspect = _load().inspect_result
    done = SimpleNamespace(
        dispatched=True,
        processes_stopped=True,
        outcome="completed",
        exit_code=0,
        isolation_identity="bwrap",
    )
    report = inspect(
        done,
        {
            "audit_complete": True,
            "per_pid_kernel_exit_accounting": True,
            "per_pid_kernel_exit_count": 3,
            "trace_integrity": {"unpaired_syscalls": 0},
        },
    )
    assert report["qualified_smoke"]
    for state in (
        None,
        {},
        {"audit_complete": True},
        {
            "audit_complete": True,
            "per_pid_kernel_exit_accounting": True,
            "per_pid_kernel_exit_count": 2,
            "trace_integrity": {"unpaired_syscalls": 0},
        },
        {
            "audit_complete": True,
            "per_pid_kernel_exit_accounting": True,
            "per_pid_kernel_exit_count": 10,
            "trace_integrity": {"unpaired_syscalls": 1},
        },
    ):
        assert not inspect(done, state)["qualified_smoke"]
    assert not inspect(
        SimpleNamespace(**{**vars(done), "processes_stopped": False}),
        {
            "audit_complete": True,
            "per_pid_kernel_exit_accounting": True,
            "per_pid_kernel_exit_count": 10,
            "trace_integrity": {"unpaired_syscalls": 0},
        },
    )["qualified_smoke"]


def test_explicit_scoped_mode_refuses_a_missing_endpoint(monkeypatch, tmp_path, capsys):
    smoke = _load()

    def config(*, scoped_worker):
        assert scoped_worker
        return SimpleNamespace(scoped_launcher=None)

    monkeypatch.setattr(smoke.WorkerMonitorConfig, "from_environment", config)
    fields = {
        "rootfs": tmp_path / "rootfs",
        "home": tmp_path / "home",
        "model-staging": tmp_path / "staging",
        "shared-models": tmp_path / "models",
        "shared-tools": tmp_path / "tools",
        "image-digest": "1" * 64,
        "qualification-sha256": "2" * 64,
        "report": tmp_path / "worker.json",
    }
    assert (
        smoke.main(
            [
                "--scoped-worker-launcher",
                *(item for name, value in fields.items() for item in (f"--{name}", str(value))),
            ]
        )
        == 1
    )
    assert "CapabilityUnavailableError" in capsys.readouterr().err
    assert not (tmp_path / "worker.json").exists()
