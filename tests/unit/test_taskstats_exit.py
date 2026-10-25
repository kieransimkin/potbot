"""Offline kernel wire protocol / exit coverage and sandbox monitor regression tests."""
from __future__ import annotations

import ctypes
import struct
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from potbot.domain.errors import CapabilityUnavailableError
from potbot.tools.taskstats_exit import (
    ExitAccounting, TaskstatsExitStream, _GENL, _NLMSG, _Taskstats, _attrs,
    _decode_exit, _netlink_messages, _pack_attr, _taskstats_records,
    check_taskstats_layout,
)
from potbot.tools.worker_audit import process_tree, write_process_tree


def _fixture(pid=113, *, start=None, version=14, read_bytes=650, write_bytes=430):
    raw = _Taskstats()
    raw.version = version
    raw.ac_pid = pid
    raw.ac_tgid = pid
    raw.ac_ppid = 1
    raw.ac_btime64 = int(time.time()) if start is None else start
    raw.ac_etime = 912345
    raw.ac_utime = 31000
    raw.ac_stime = 22000
    raw.hiwater_rss = 37  # UAPI: KiB
    raw.read_bytes = read_bytes
    raw.write_bytes = write_bytes
    raw.read_char = 33
    raw.write_char = 44
    raw.ac_minflt = 9
    raw.ac_majflt = 2
    return bytes(raw)


def _event(pid=113):
    nested = _pack_attr(1, struct.pack("=I", pid)) + _pack_attr(3, _fixture(pid))
    attrs = _pack_attr(4, nested)
    body = _GENL.pack(2, 1, 0) + attrs
    return _NLMSG.pack(_NLMSG.size + len(body), 92, 0, 0, 0) + body


def test_taskstats_struct_matches_current_linux_uapi():
    check_taskstats_layout()
    assert ctypes.sizeof(_Taskstats) == 432
    assert _Taskstats.ac_pid.offset == 128
    assert _Taskstats.hiwater_rss.offset == 200
    assert _Taskstats.write_bytes.offset == 256


def test_kernel_exit_event_parses_per_task_not_aggregate_tgid():
    events = _taskstats_records(_event(), 92)
    assert len(events) == 1
    stats = events[0].public_private_record()
    assert stats['pid'] == 113
    assert stats['cpu_total_usec'] == 53000
    assert stats['rss_peak_bytes'] == 37 * 1024
    assert stats['block_read_bytes'] == 650
    assert stats['block_write_bytes'] == 430
    assert 'command' not in stats and 'uid' not in stats


def test_rejects_incomplete_kernel_event_and_unsupported_versions():
    with pytest.raises(CapabilityUnavailableError):
        _decode_exit(_fixture(version=3))
    with pytest.raises(CapabilityUnavailableError):
        _decode_exit(_fixture()[:200])
    payload = _pack_attr(4, _pack_attr(1, struct.pack('=I', 113)))
    body = _GENL.pack(2, 1, 0) + payload
    raw = _NLMSG.pack(16 + len(body), 92, 0, 0, 0) + body
    with pytest.raises(CapabilityUnavailableError):
        _taskstats_records(raw, 92)
    with pytest.raises(CapabilityUnavailableError):
        list(_attrs(b'\x00\x00\x01\x00'))
    with pytest.raises(CapabilityUnavailableError):
        list(_netlink_messages(b'\x01\x02'))


def _collector(records):
    stream = TaskstatsExitStream.__new__(TaskstatsExitStream)
    stream._records = records
    stream._start_wall = time.time() - 1
    stream._failure = None
    stream._lock = threading.Lock()
    stream._registered = True
    stream._thread = type("ReceiverStatus", (), {"is_alive": lambda self: True})()
    return stream


def test_every_traced_pid_has_exact_one_kernel_exit_record():
    exits = _taskstats_records(_event(113), 92)
    stream = _collector({113: exits})
    assert stream.coverage({113}, deadline_seconds=0)[113].cpu_user_usec == 31000
    with pytest.raises(CapabilityUnavailableError, match='missing'):
        stream.coverage({113, 114}, deadline_seconds=0)
    stream._records[113].append(exits[0])
    with pytest.raises(CapabilityUnavailableError, match='reuse|duplicate'):
        stream.coverage({113}, deadline_seconds=0)
    stream._records[113] = [replace(exits[0], started_seconds=int(stream._start_wall) - 30)]
    with pytest.raises(CapabilityUnavailableError, match='birth'):
        stream.coverage({113}, deadline_seconds=0)
    stream._failure = 'NetlinkOverflow'
    with pytest.raises(CapabilityUnavailableError):
        stream.coverage({113}, deadline_seconds=0)


def test_kernel_exit_attached_to_fork_tree_and_missing_data_denied(tmp_path: Path):
    t1, t2 = tmp_path/'trace.113', tmp_path/'trace.114'
    t1.write_text('1793000000.001 fork() = 114 <0.0001>\n')
    t2.write_text('1793000000.002 execve("/usr/bin/true", ["true"], NULL) = 0 <0.0001>\n')
    record = _taskstats_records(_event(113), 92)[0]
    child = replace(record, pid=114, tgid=114, ppid=113)
    tree = process_tree([t1, t2], kernel_exit={113: record, 114: child})
    assert tree['tree_complete']
    assert tree['exit_resource_fidelity'] == 'kernel_taskstats_exit'
    assert all(n['resource_fidelity'] == 'kernel_taskstats_exit' for n in tree['nodes'])
    assert tree['nodes'][0]['kernel_exit_resources']['cpu_user_usec'] == 31000
    output=write_process_tree(tree,tmp_path/'kernel-tree.jsonl')
    assert output['sha256'] and output['nodes'] == 2
    with pytest.raises(CapabilityUnavailableError):
        process_tree([t1,t2],kernel_exit={113: record})


def test_worker_command_registers_kernel_collector_before_returning_launch(tmp_path: Path, monkeypatch):
    import hashlib
    from types import SimpleNamespace
    from uuid import uuid4
    from potbot.tools import worker_telemetry as module

    audit = tmp_path / 'audit';audit.mkdir(mode=0o700)
    groups = tmp_path / 'groups';groups.mkdir()
    tracer=tmp_path/'strace';tracer.write_bytes(b'fake-tracer')
    helper=tmp_path/'join';helper.write_bytes(b'fake-helper')
    calls=[]
    class Stream:
        healthy=True
        def open(self): calls.append('registered')
        def close(self): calls.append('closed')
    monkeypatch.setattr(module, 'TaskstatsExitStream', Stream)
    monkeypatch.setattr(module, '_cgroup_usage', lambda _: {'cpu_usec':0})
    config=SimpleNamespace(audit_root=audit,cgroup_root=groups,strace=tracer,
                           cgroup_join=helper,strace_sha256=hashlib.sha256(tracer.read_bytes()).hexdigest(),
                           cgroup_join_sha256=hashlib.sha256(helper.read_bytes()).hexdigest())
    session=module.WorkerMonitorSession(config,uuid4(),uuid4(),'pinned-image')
    argv=session.command(('/usr/bin/true',))
    assert calls==['registered']
    assert argv[0]==str(helper) and str(tracer) in argv
    assert session.healthy is False  # No worker poll thread exists yet.
    session._kernel.close()
    with pytest.raises(CapabilityUnavailableError):
        session.command(('/usr/bin/true',))


def test_dead_kernel_listener_never_qualifies_complete_worker_accounting():
    record = _taskstats_records(_event(113), 92)[0]
    stream = _collector({113: [record]})
    stream._thread = type("DeadReceiver", (), {"is_alive": lambda self: False})()
    with pytest.raises(CapabilityUnavailableError, match="channel unavailable"):
        stream.coverage({113}, deadline_seconds=0)
