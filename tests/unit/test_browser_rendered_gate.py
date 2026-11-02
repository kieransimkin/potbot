"""Fail-closed rendered acceptance checks, independent of browsers and databases."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_browser_rendered.py"
SPEC = importlib.util.spec_from_file_location("browser_rendered_gate", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


def passing_report(profile="playwright", version="1.64.1"):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", errors="0", failures="0", skipped="0", tests="5")
    for name in sorted(GATE.EXPECTED_TESTS):
        case = ET.SubElement(
            suite, "testcase", name=name, classname="tests.browser.test_rendered_worker"
        )
        if name == "test_accessibility_role_actions_and_verified_render_change":
            props = ET.SubElement(case, "properties")
            for key, value in {
                "browser_profile": profile,
                "browser_version": "144.0.7559.96",
                "browser_binary": json.dumps(
                    {
                        "source": "engine",
                        "sha256": "a" * 64,
                        "bytes": 1234,
                    }
                ),
                "packages": json.dumps({name: version for name in GATE.PROFILES[profile]}),
            }.items():
                ET.SubElement(props, "property", name=key, value=value)
    return root


def encode(root):
    return ET.tostring(root, encoding="utf-8")


def test_accepts_five_real_cases_with_explicit_profile_metadata():
    result = GATE.validate_report(encode(passing_report()), "playwright")
    assert result["test_count"] == 5
    assert result["packages"] == {"playwright": "1.64.1"}


@pytest.mark.parametrize("tag", ["failure", "error", "skipped"])
def test_rejects_failure_skip_or_collection_error_even_with_zero_counters(tag):
    root = passing_report()
    ET.SubElement(root.find("testsuite/testcase"), tag)
    with pytest.raises(ValueError):
        GATE.validate_report(encode(root), "playwright")


@pytest.mark.parametrize("attribute", ["errors", "failures", "skipped", "tests"])
def test_rejects_counter_claims_that_do_not_match_success(attribute):
    root = passing_report()
    root.find("testsuite").set(attribute, "1")
    with pytest.raises(ValueError):
        GATE.validate_report(encode(root), "playwright")


def test_rejects_duplicate_names_even_when_total_is_five():
    root = passing_report()
    cases = root.findall("testsuite/testcase")
    cases[1].set("name", cases[0].get("name"))
    with pytest.raises(ValueError):
        GATE.validate_report(encode(root), "playwright")


def test_rejects_missing_or_duplicate_runtime_identity_properties():
    root = passing_report()
    props = root.find("testsuite/testcase/properties")
    ET.SubElement(props, "property", name="browser_profile", value="playwright")
    with pytest.raises(ValueError):
        GATE.validate_report(encode(root), "playwright")
    root = passing_report()
    props = root.find("testsuite/testcase/properties")
    props.remove(props[0])
    with pytest.raises(ValueError):
        GATE.validate_report(encode(root), "playwright")


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not XML",
        b"x" * (4 * 1024 * 1024 + 1),
        b"<!DOCTYPE x><testsuites/>",
        b'<!ENTITY x "bad">',
    ],
    ids=["empty", "malformed", "oversized", "doctype", "entity"],
)
def test_rejects_unbounded_malformed_or_entity_xml(raw):
    with pytest.raises(ValueError):
        GATE.validate_report(raw, "playwright")


def test_a_playwright_pass_cannot_be_relabeled_patchright():
    with pytest.raises(ValueError):
        GATE.validate_report(encode(passing_report()), "patchright")


def source_fixture(tmp_path):
    for name in GATE.SOURCE_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic gate unit-test source\n")
    (tmp_path / "sandbox/image/Dockerfile").write_text(
        "playwright==1.64.1 patchright==1.64.2 playwright-stealth==2.0.3\n"
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "synthetic gate fixture",
        ],
        check=True,
    )
    report = tmp_path / "input.xml"
    report.write_bytes(encode(passing_report()))
    return report


def test_successful_gate_cannot_claim_other_qualification(tmp_path):
    report = source_fixture(tmp_path)
    result = GATE.verify(tmp_path, report, "playwright", require_image_pins=True)
    assert result["rendered_worker_passed"] is True
    assert result["dependency_pins_match_image"] is True
    assert result["tested_sources_dirty"] is False
    for name in (
        "xpra_qualified",
        "sandbox_image_qualified",
        "kernel_isolation_qualified",
        "multi_agent_qualified",
        "postgresql_controller_qualified",
    ):
        assert result[name] is False
    assert set(result["source_sha256"]) == set(GATE.SOURCE_FILES)


def test_wrong_dependency_pin_is_reported_and_can_be_a_required_failure(tmp_path):
    report = source_fixture(tmp_path)
    report.write_bytes(encode(passing_report(version="1.57.0")))
    assert GATE.verify(tmp_path, report, "playwright")["dependency_pins_match_image"] is False
    with pytest.raises(ValueError, match="exact package pins"):
        GATE.verify(tmp_path, report, "playwright", require_image_pins=True)


def test_source_changes_are_visible_and_missing_source_fails(tmp_path):
    report = source_fixture(tmp_path)
    file = tmp_path / GATE.SOURCE_FILES[0]
    before = GATE.verify(tmp_path, report, "playwright")
    file.write_text("changed fixture source\n")
    after = GATE.verify(tmp_path, report, "playwright")
    assert after["tested_sources_dirty"] is True
    assert before["source_sha256"] != after["source_sha256"]
    file.unlink()
    with pytest.raises(ValueError, match="source is missing"):
        GATE.verify(tmp_path, report, "playwright")


def test_failed_cli_recheck_does_not_leave_a_stale_success_file(tmp_path):
    report = source_fixture(tmp_path)
    report.write_text("broken XML")
    output = tmp_path / "result.json"
    output.write_text('{"rendered_worker_passed":true}')
    process = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(report),
            "--root",
            str(tmp_path),
            "--profile",
            "playwright",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert process.returncode == 1
    assert not output.exists()


@pytest.mark.parametrize(
    "key,value",
    [
        ("source", "arbitrary"),
        ("source", None),
        ("source", []),
        ("sha256", "A" * 64),
        ("sha256", "0" * 63),
        ("sha256", None),
        ("bytes", True),
        ("bytes", 0),
        ("bytes", -1),
        ("bytes", 2 * 1024**3 + 1),
        ("bytes", 1.5),
    ],
)
def test_rejects_invalid_executable_identity(key, value):
    root = passing_report()
    prop = root.find("testsuite/testcase/properties/property[@name='browser_binary']")
    identity = json.loads(prop.get("value"))
    identity[key] = value
    prop.set("value", json.dumps(identity))
    with pytest.raises(ValueError, match="executable identity"):
        GATE.validate_report(encode(root), "playwright")


@pytest.mark.parametrize(
    "value",
    [
        "{}",
        "[]",
        "null",
        '{"source":"engine","source":"system","sha256":"a","bytes":1}',
    ],
)
def test_rejects_missing_or_ambiguous_executable_identity(value):
    root = passing_report()
    root.find("testsuite/testcase/properties/property[@name='browser_binary']").set("value", value)
    with pytest.raises(ValueError):
        GATE.validate_report(encode(root), "playwright")


def test_ci_gate_rejects_system_browser_evidence(tmp_path):
    report = source_fixture(tmp_path)
    root = passing_report()
    prop = root.find("testsuite/testcase/properties/property[@name='browser_binary']")
    identity = json.loads(prop.get("value"))
    identity["source"] = "system"
    prop.set("value", json.dumps(identity))
    report.write_bytes(encode(root))
    assert GATE.verify(tmp_path, report, "playwright")["browser_binary"]["source"] == "system"
    with pytest.raises(ValueError, match="engine's executable"):
        GATE.verify(tmp_path, report, "playwright", require_engine_binary=True)


def test_engine_gate_preserves_executable_hash_without_claiming_image_qualification(tmp_path):
    report = source_fixture(tmp_path)
    result = GATE.verify(tmp_path, report, "playwright", require_engine_binary=True)
    assert result["browser_binary"] == {"source": "engine", "sha256": "a" * 64, "bytes": 1234}
    assert result["sandbox_image_qualified"] is False


def test_duplicate_package_identity_is_rejected():
    root = passing_report()
    root.find("testsuite/testcase/properties/property[@name='packages']").set(
        "value",
        '{"playwright":"1.64.1","playwright":"1.64.1"}',
    )
    with pytest.raises(ValueError):
        GATE.validate_report(encode(root), "playwright")
