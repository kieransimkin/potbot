"""Release-gate regressions: never convert missing/invalid evidence into success."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

_SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'qualify_operator_workers.py'
_spec = importlib.util.spec_from_file_location('operator_worker_qualification', _SCRIPT)
assert _spec and _spec.loader
_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gate)


def test_zero_exit_without_attestation_is_rejected(monkeypatch):
    monkeypatch.setattr(_gate.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='garbage', stderr=''))
    assert _gate.run_command(['ignored'], timeout=5)['passed'] is False


def test_empty_output_is_rejected(monkeypatch):
    monkeypatch.setattr(_gate.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='', stderr=''))
    assert _gate.run_command(['ignored'], timeout=5)['passed'] is False


def test_multiple_reports_are_rejected(monkeypatch):
    monkeypatch.setattr(_gate.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='{}\n{"qualified":true}', stderr=''))
    assert _gate.run_command(['ignored'], timeout=5)['passed'] is False


def test_explicit_success_field_and_status_required(monkeypatch):
    result = SimpleNamespace(returncode=0, stdout=json.dumps({'qualified_smoke': True}), stderr='')
    monkeypatch.setattr(_gate.subprocess, 'run', lambda *a, **k: result)
    assert _gate.run_command(['ignored'], timeout=5, success_field='qualified_smoke')['passed'] is True
    assert _gate.run_command(['ignored'], timeout=5)['passed'] is False
    result.returncode = 1
    assert _gate.run_command(['ignored'], timeout=5, success_field='qualified_smoke')['passed'] is False
