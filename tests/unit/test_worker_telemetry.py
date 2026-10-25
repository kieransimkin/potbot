"""Operator telemetry: parser, host accounting and privacy/fail-closed contracts."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
from potbot.tools.worker_telemetry import (
    _UtilisationWindow, _cgroup_usage, _safe_read, _safe_write,
    _trace_events, _private_root, _composite_load, OperatorTelemetryReader,
)
from potbot.domain.errors import ScopeDeniedError
from collections import Counter


def test_trace_parser_records_pid_and_access_mode_without_disclosing_buffers(tmp_path: Path):
    trace = tmp_path / 'trace.12345'
    trace.write_text('''1792130000.123456 openat(AT_FDCWD, "/home/agent/input.txt", O_RDONLY|O_CLOEXEC) = 3</home/agent/input.txt> <0.00001>\n1792130000.123457 read(3</home/agent/input.txt>, "SECRET", 6) = 6 <0.00001>\n1792130000.123458 openat(AT_FDCWD, "/home/agent/out.bin", O_RDWR|O_CREAT, 0600) = 4</home/agent/out.bin> <0.00001>\n1792130000.123459 write(4</home/agent/out.bin>, "SECRET", 6) = 6 <0.00001>\n1792130000.123460 execve("/usr/bin/python3", ["python3"], 0x7ffd) = 0 <0.0001>\n''')
    output, counts = io.StringIO(), Counter()
    n = _trace_events(trace, 12345, output, counts)
    rows = [json.loads(x) for x in output.getvalue().splitlines()]
    assert n == 5
    assert counts['openat'] == 2 and counts['write'] == 1
    assert {(r['path'], r['mode']) for r in rows} >= {
        ('/home/agent/input.txt', 'read'), ('/home/agent/out.bin', 'read_write'),
        ('/home/agent/out.bin', 'write'), ('/usr/bin/python3', 'execute')}
    assert 'SECRET' not in output.getvalue()  # only raw operator-only trace contains syscall buffers
    assert {row['pid'] for row in rows} == {12345}


def test_failed_syscall_is_reported_not_claimed_as_access(tmp_path: Path):
    trace = tmp_path / 'trace.11'
    trace.write_text('1792130000.123456 openat(AT_FDCWD, "/missing", O_RDONLY) = -1 ENOENT <0.0001>\n')
    output = io.StringIO()
    assert _trace_events(trace, 11, output, Counter()) == 1
    assert json.loads(output.getvalue())['result_error'] is True


def test_strict_private_writes_and_no_follow(tmp_path: Path):
    root = tmp_path / 'audit'
    root.mkdir(mode=0o700)
    assert _private_root(root) == root
    head = root / 'live.json'
    _safe_write(head, {'schema': 1, 'state': 'running'})
    assert _safe_read(head)['schema'] == 1
    assert head.stat().st_mode & 0o077 == 0
    head.unlink()
    head.symlink_to(tmp_path / 'victim')
    with pytest.raises(OSError): _safe_read(head)
    with pytest.raises(ScopeDeniedError): _safe_write(head, {})
    root.chmod(0o777)
    with pytest.raises(ScopeDeniedError): _private_root(root)


def test_kernel_cgroup_lifetime_counter_parser(tmp_path: Path):
    (tmp_path / 'cpu.stat').write_text('usage_usec 101\nuser_usec 70\nsystem_usec 31\n')
    (tmp_path / 'io.stat').write_text('8:0 rbytes=12 wbytes=9 rios=1 wios=1\n8:1 rbytes=8 wbytes=4\n')
    (tmp_path / 'memory.current').write_text('450\n')
    (tmp_path / 'memory.peak').write_text('800\n')
    (tmp_path / 'pids.current').write_text('0\n')
    assert _cgroup_usage(tmp_path) == {
      'cpu_usec': 101, 'user_usec': 70, 'system_usec': 31,
      'read_bytes': 20, 'write_bytes': 13, 'memory_current_bytes': 450,
      'memory_peak_bytes': 800, 'pids_current': 0}


def test_three_minute_and_spike_weighting():
    win = _UtilisationWindow()
    win.put(0, 0.2)
    win.put(100, 0.2)
    win.put(101, 1.0)
    assert win.average(2) > win.average(180)
    for t in range(102, 400): win.put(t, 1.0)
    assert 0.99 < win.average(180) <= 1


def test_global_reader_never_exposes_private_paths_or_foreign_agents(tmp_path: Path):
    import uuid
    workspace = uuid.uuid4()
    other = uuid.uuid4()
    root = tmp_path / 'audit'
    root.mkdir(mode=0o700)
    for w in (workspace, other):
        worker_dir = root / str(w) / str(uuid.uuid4())
        worker_dir.mkdir(parents=True, mode=0o700)
        _safe_write(worker_dir / 'live.json', {
            'schema': 1, 'state': 'running', 'workspace_id': str(w),
            'agent_id': str(uuid.uuid4()), 'worker_id': str(uuid.uuid4()),
            'sampled_at': __import__('time').time(),
            'usage': {'cpu_usec': 10, 'pids_current': 1, 'memory_current_bytes': 100}})
    reader = OperatorTelemetryReader(root)
    report = reader.snapshot(workspace)
    assert report['schema'] == 1
    assert len(report['workers']) == 1 and len(report['agents']) == 1
    assert str(other) not in json.dumps(report)
    assert '/proc/' not in json.dumps(report)
    assert set(report['system']) >= {'load3m', 'load2s', 'pressure', 'memoryUsed'}


def test_system_pressure_contributes_to_total_load_not_just_cpu():
    assert _composite_load(.12, {"cpu": {"avg10": 12.0}, "io": {"avg10": 85.0}}) == .85
    assert _composite_load(.91, {"cpu": {"avg10": 40.0}, "memory": {"avg10": 50.0}}) == .91
    assert _composite_load(0, {}) == 0
