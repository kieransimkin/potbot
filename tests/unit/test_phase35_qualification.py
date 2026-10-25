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
    done = SimpleNamespace(dispatched=True, processes_stopped=True,
                           outcome="completed", exit_code=0, isolation_identity="bwrap")
    report = inspect(done, {"audit_complete": True, "per_pid_kernel_exit_accounting": True,
                            "per_pid_kernel_exit_count": 3, "trace_integrity": {"unpaired_syscalls": 0}})
    assert report["qualified_smoke"]
    for state in (
        None, {}, {"audit_complete": True},
        {"audit_complete": True, "per_pid_kernel_exit_accounting": True,
         "per_pid_kernel_exit_count": 2, "trace_integrity": {"unpaired_syscalls": 0}},
        {"audit_complete": True, "per_pid_kernel_exit_accounting": True,
         "per_pid_kernel_exit_count": 10, "trace_integrity": {"unpaired_syscalls": 1}},
    ):
        assert not inspect(done, state)["qualified_smoke"]
    assert not inspect(SimpleNamespace(**{**vars(done), "processes_stopped": False}),
                       {"audit_complete": True, "per_pid_kernel_exit_accounting": True,
                        "per_pid_kernel_exit_count": 10, "trace_integrity": {"unpaired_syscalls": 0}})["qualified_smoke"]
