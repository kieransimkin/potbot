"""Bounded probe-wire tests; subprocesses here use --wire-only, not a sandbox."""

from __future__ import annotations

import errno
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "sandbox/qualification/worker_probe.py"
spec = importlib.util.spec_from_file_location("worker_probe_s46", PROBE)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def request():
    return {
        "schema_version": 1,
        "operation_ref": probe.OPERATION,
        "invocation_id": str(uuid4()),
        "input": {"type_ref": probe.INPUT, "value": {"nonce": "a" * 64}},
    }


def test_wire_only_subprocess_echoes_bound_nonce_without_claiming_inspection():
    value = request()
    result = subprocess.run(
        [sys.executable, str(PROBE), "--wire-only"],
        input=json.dumps(value).encode(),
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0 and not result.stderr
    output = json.loads(result.stdout)
    assert output["type_ref"] == probe.OUTPUT
    assert output["value"]["invocation_id"] == value["invocation_id"]
    assert output["value"]["nonce"] == value["input"]["value"]["nonce"]
    assert output["value"]["mode"] == "wire_only"
    assert output["value"]["observation"] is None


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(schema_version=True),
        lambda r: r.update(extra="not admitted"),
        lambda r: r.update(operation_ref="syn://thing/arbitrary-worker"),
        lambda r: r.update(invocation_id="other"),
        lambda r: r["input"].update(type_ref="syn://type/foreign"),
        lambda r: r["input"]["value"].update(nonce="short"),
        lambda r: r["input"]["value"].update(argv=["/bin/sh"]),
    ],
)
def test_probe_rejects_changed_transport_or_untrusted_commands(change):
    value = request()
    change(value)
    with pytest.raises((ValueError, TypeError)):
        probe.decode(json.dumps(value).encode())


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b"null", b"\xff", b"a" * 16385])
def test_malformed_stdin_never_produces_a_success_payload(raw):
    result = subprocess.run(
        [sys.executable, str(PROBE), "--wire-only"], input=raw, capture_output=True, timeout=5
    )
    assert result.returncode == 2 and not result.stdout
    assert result.stderr == b"worker qualification probe rejected\n"


def test_arbitrary_command_line_is_not_an_execution_escape():
    result = subprocess.run(
        [sys.executable, str(PROBE), "--exec", "/bin/sh"], input=b"", capture_output=True, timeout=5
    )
    assert result.returncode == 2 and not result.stdout


def test_root_readonly_requires_one_actual_root_mount():
    ro = "1 0 0:1 / / ro,relatime - ext4 /dev/test ro\n"
    rw = ro.replace("ro,relatime", "rw,relatime")
    assert probe.root_is_readonly(ro)
    assert not probe.root_is_readonly(rw)
    assert not probe.root_is_readonly(ro + ro)
    assert not probe.root_is_readonly("")


@pytest.mark.parametrize(
    "code,expected",
    [(errno.EPERM, True), (errno.EACCES, True), (errno.EAFNOSUPPORT, False), (errno.EMFILE, False)],
)
def test_network_unavailability_is_not_misreported_as_policy_denial(monkeypatch, code, expected):
    def fail(*_):
        raise OSError(code, "fixture")

    monkeypatch.setattr(probe.socket, "socket", fail)
    assert probe.socket_denied(probe.socket.AF_INET) is expected
