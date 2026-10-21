"""Required Linux qualification: these tests never turn missing isolation into a pass."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from potbot.tools.secrets import CaptureRedactor, SecretReferenceProvider
from potbot.tools.supervisor import ProcessRequest, ProcessSupervisor

pytestmark = pytest.mark.sandbox


def request(root: Path, program: str, **kwargs: object) -> ProcessRequest:
    return ProcessRequest((sys.executable, "-I", "-c", program), root, root, **kwargs)


def test_real_compile_and_test_with_literal_arguments(tmp_path: Path) -> None:
    (tmp_path / "program.py").write_text("def add(a,b): return a+b\n")
    literal = "literal; touch unexpected-file $(also-not-a-shell)"
    program = "import sys,py_compile;py_compile.compile('program.py',doraise=True);import program;assert program.add(2,3)==5;print(sys.argv[1]);print('verified',file=sys.stderr)"
    result = ProcessSupervisor().run(
        ProcessRequest(
            (sys.executable, "-I", "-c", "import sys;sys.path.insert(0,'.');" + program, literal),
            tmp_path,
            tmp_path,
        )
    )
    assert result.outcome == "completed", result
    assert result.stdout == (literal + "\n").encode()
    assert result.stderr == b"verified\n"
    assert not (tmp_path / "unexpected-file").exists()
    assert result.isolation_identity and result.dispatched
    assert result.processes_stopped


def test_parent_and_child_cannot_read_write_symlink_escape_or_network(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "private-canary"
    canary.write_text("unreadable-private-value")
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "escape").symlink_to(canary)
    program = f"""
import os,socket,subprocess,sys
for path in [{str(canary)!r},'escape']:
    for mode in ['r','w']:
        try:
            with open(path,mode) as f:
                if mode=='w':f.write('escaped')
                else:f.read()
        except OSError:pass
        else:raise AssertionError('filesystem escape')
try:socket.socket().connect(('1.1.1.1',443))
except OSError:pass
else:raise AssertionError('network escape')
child="import pathlib,socket; p=pathlib.Path({str(canary)!r}); assert not p.exists(); socket.socket()"
r=subprocess.run([sys.executable,'-I','-c',child],capture_output=True)
assert r.returncode!=0 and b'PermissionError' in r.stderr
open('permitted','w').write('done')
print('isolation verified')
"""
    result = ProcessSupervisor().run(request(candidate, program))
    assert result.outcome == "completed", result
    assert result.stdout == b"isolation verified\n"
    assert canary.read_text() == "unreadable-private-value"
    assert (candidate / "permitted").read_text() == "done"


def test_deadline_kills_detached_descendant_and_keeps_partial_capture(tmp_path: Path) -> None:
    child = "import time,pathlib\nwhile True:\n pathlib.Path('heartbeat').write_text(str(time.monotonic()))\n time.sleep(.02)"
    program = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-I','-c',{child!r}],start_new_session=True);print('started',flush=True);time.sleep(120)"
    result = ProcessSupervisor().run(request(tmp_path, program, timeout_ms=400))
    assert result.outcome == "timeout", result
    assert result.elapsed_ms < 3000
    assert b"started" in result.stdout
    assert result.dispatched
    assert result.processes_stopped
    heartbeat = (tmp_path / "heartbeat").read_bytes()
    time.sleep(0.15)
    assert (tmp_path / "heartbeat").read_bytes() == heartbeat


def test_cancellation_and_distinct_nonzero_result(tmp_path: Path) -> None:
    started = time.monotonic()
    result = ProcessSupervisor().run(
        request(tmp_path, "import time;time.sleep(120)"),
        cancelled=lambda: time.monotonic() - started > 0.2,
    )
    assert result.outcome == "cancelled", result
    assert result.elapsed_ms < 3000
    ordinary = ProcessSupervisor().run(request(tmp_path, "raise SystemExit(7)"))
    assert ordinary.outcome == "nonzero_exit" and ordinary.exit_code == 7
    assert not ordinary.retryable


def test_secret_injection_redaction_output_bound_and_policy_failure(tmp_path: Path) -> None:
    secret = "capture-canary-7d81c060"
    material = SecretReferenceProvider({"build/key": secret}).resolve(
        {"BUILD_KEY": "build/key"},
        permitted_references=frozenset({"build/key"}),
        permitted_environment=frozenset({"BUILD_KEY"}),
    )
    program = "import os,sys;assert os.environ['BUILD_KEY'];print(os.environ['BUILD_KEY']);print(os.environ['BUILD_KEY'],file=sys.stderr);print('x'*50000)"
    result = ProcessSupervisor().run(
        request(tmp_path, program, environment=material.environment, max_output_bytes=128),
        redactor=material.redactor,
    )
    assert result.outcome == "completed", result
    assert secret.encode() not in result.stdout + result.stderr
    assert b"[REDACTED]" in result.stdout and b"[REDACTED]" in result.stderr
    assert result.stdout_truncated and len(result.stdout) <= 128
    failed = ProcessSupervisor().run(
        request(tmp_path, "print('must-reject')"),
        redactor=CaptureRedactor(reject_patterns=(b"must-reject",)),
    )
    assert failed.outcome == "inconclusive" and failed.error_code == "tool_failure"
    assert b"must-reject" not in failed.stdout + failed.stderr


def test_unconfirmed_wait_cannot_claim_stopped_processes(tmp_path: Path, monkeypatch) -> None:
    import potbot.tools.supervisor as supervisor_module

    original_popen = subprocess.Popen
    cleanup = []

    def create(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        if "--json-status-fd" in args[0]:
            cleanup.append(process.wait)

            def cannot_confirm(timeout=None):
                raise subprocess.TimeoutExpired(process.args, timeout)

            process.wait = cannot_confirm
        return process

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", create)
    try:
        result = ProcessSupervisor().run(request(tmp_path, "print('finished')"))
        assert result.outcome == "inconclusive"
        assert result.error_code == "indeterminate"
        assert not result.processes_stopped
    finally:
        for wait in cleanup:
            wait(timeout=3)
