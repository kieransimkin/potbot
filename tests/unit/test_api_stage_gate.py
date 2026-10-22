"""T02.4 verification reporting is explicitly not security attestation."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _stage_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "tool_phase2_4_gate.py"
    spec = importlib.util.spec_from_file_location("tool_phase2_4_gate_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_xml_is_not_a_pass(tmp_path: Path) -> None:
    module = _stage_module()
    assert module.suite_stats(tmp_path / "not-found.xml")["errors"] == 1


def test_junit_skips_are_counted(tmp_path: Path) -> None:
    module = _stage_module()
    path = tmp_path / "results.xml"
    path.write_text('<testsuite name="test" tests="4" failures="0" errors="0" skipped="2"/>')
    assert module.suite_stats(path) == {"tests": 4, "failures": 0, "errors": 0, "skipped": 2}


def test_successful_fixtures_do_not_imply_production_qualification(tmp_path: Path, monkeypatch) -> None:
    module = _stage_module()
    for name in module.SOURCES:
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("fixture")

    def fake_run(argv, **_):
        report = next(item.split("=", 1)[1] for item in argv if item.startswith("--junitxml="))
        Path(report).write_text('<testsuite tests="5" errors="0" failures="0" skipped="0"/>')
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    output = module.run(mode="offline", root=tmp_path, output=tmp_path / "gate.json")
    assert output["fixtures_passed"] is True
    assert output["all_real_service_fixtures_exercised"] is True
    assert output["production_egress_qualified"] is False
    assert output["durable_api_dispatch_qualified"] is False
    assert output["stage_qualified"] is False
