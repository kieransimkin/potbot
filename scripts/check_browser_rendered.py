"""Validate rendered-worker JUnit evidence without asserting Xpra/OS qualification."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

EXPECTED_TESTS = frozenset({
    "test_accessibility_role_actions_and_verified_render_change",
    "test_picker_click_maps_css_script_and_accessibility_without_activating",
    "test_keyboard_pointer_cdp_and_exact_locator_rejection",
    "test_two_real_browser_contexts_do_not_share_form_or_picker_state",
    "test_real_worker_stdio_rejects_duplicate_and_nonfinite_frames",
})
PROFILES = {
    "playwright": ("playwright",),
    "patchright": ("patchright",),
    "playwright-stealth": ("playwright", "playwright-stealth"),
    "patchright-stealth": ("patchright", "playwright-stealth"),
}
SOURCE_FILES = (
    "src/potbot/tools/xpra_browser_worker.py",
    "src/potbot/tools/browser_https_routes.py",
    "src/potbot/tools/semantic_contracts.py",
    "src/potbot/tools/browser_cog_contracts.py",
    "tests/browser/test_rendered_worker.py",
    "tests/browser/fixtures/probe.html",
    "tests/browser/fixtures/probe.css",
    "tests/browser/fixtures/probe.js",
    "scripts/check_browser_rendered.py",
    "scripts/browser_rendered_runtime.py",
    "sandbox/image/Dockerfile",
    ".github/workflows/browser-rendered-acceptance.yml",
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate browser identity field")
        result[key] = value
    return result


def validate_report(raw: bytes, profile: str) -> dict:
    """Reject missing/duplicate cases, skips, collection errors and lost metadata."""
    if profile not in PROFILES or not 0 < len(raw) <= 4 * 1024 * 1024:
        raise ValueError("invalid browser profile or report size")
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("DTD/entity declarations are not acceptance evidence")
    try:
        tree = ElementTree.fromstring(raw)
    except ElementTree.ParseError as error:
        raise ValueError("invalid browser acceptance XML") from error
    if tree.tag != "testsuites":
        raise ValueError("expected a pytest JUnit testsuites report")
    suites = list(tree.iter("testsuite"))
    if len(suites) != 1 or suites[0].get("tests") != str(len(EXPECTED_TESTS)):
        raise ValueError("browser acceptance suite count changed")
    if any(suites[0].get(key) != "0" for key in ("errors", "failures", "skipped")):
        raise ValueError("browser acceptance must complete without errors, failures or skips")
    if any(node.tag in {"error", "failure", "skipped"} for node in tree.iter()):
        raise ValueError("browser acceptance contains a failed or skipped case")
    cases = list(tree.iter("testcase"))
    if len(cases) != len(EXPECTED_TESTS) or {c.get("name") for c in cases} != EXPECTED_TESTS:
        raise ValueError("missing, duplicate or unexpected browser acceptance case")
    for case in cases:
        if not case.get("classname", "").endswith("test_rendered_worker"):
            raise ValueError("browser case belongs to an unexpected suite")
    first = next(c for c in cases if c.get("name") ==
                 "test_accessibility_role_actions_and_verified_render_change")
    props = {}
    for prop in first.findall("./properties/property"):
        name = prop.get("name")
        if name in props:
            raise ValueError("duplicate browser version metadata")
        props[name] = prop.get("value")
    if set(props) != {"browser_profile", "browser_version", "packages", "browser_binary"}:
        raise ValueError("browser identity metadata is incomplete")
    if props["browser_profile"] != profile or not re.fullmatch(
        r"[0-9]+(?:\.[0-9]+){1,4}", props["browser_version"] or ""
    ):
        raise ValueError("selected browser profile/version does not match evidence")
    try:
        packages = json.loads(props["packages"], object_pairs_hook=_unique_object)
        binary = json.loads(props["browser_binary"], object_pairs_hook=_unique_object)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid package metadata") from error
    if not isinstance(packages, dict) or set(packages) != set(PROFILES[profile]) or any(
        not isinstance(v, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", v) is None
        for v in packages.values()
    ):
        raise ValueError("selected package versions are absent or invalid")
    if not isinstance(binary, dict) or set(binary) != {"source", "sha256", "bytes"}:
        raise ValueError("browser executable identity is incomplete")
    if (binary["source"] not in ("system", "engine")
            or not isinstance(binary["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", binary["sha256"]) is None
            or type(binary["bytes"]) is not int
            or not 0 < binary["bytes"] <= 2 * 1024**3):
        raise ValueError("invalid browser executable identity")
    return {
        "browser_binary": binary,
        "profile": profile, "browser_version": props["browser_version"],
        "packages": packages, "test_count": len(cases),
    }


def image_pins(root: Path) -> dict[str, str]:
    text = (root / "sandbox/image/Dockerfile").read_text(encoding="utf-8")
    pairs = re.findall(r"\b(playwright-stealth|playwright|patchright)==([0-9.]+)", text)
    pins = dict(pairs)
    if len(pairs) != 3 or set(pins) != {"playwright", "patchright", "playwright-stealth"}:
        raise ValueError("sandbox image must declare exactly three browser package pins")
    return pins


def verify(root: Path, report: Path, profile: str, *, require_image_pins: bool = False,
           require_engine_binary: bool = False) -> dict:
    with report.open("rb") as stream:
        raw = stream.read(4 * 1024 * 1024 + 1)
    identity = validate_report(raw, profile)
    if require_engine_binary and identity["browser_binary"]["source"] != "engine":
        raise ValueError("rendered CI must use the selected engine's executable")
    pins = image_pins(root)
    matches = all(pins[name] == value for name, value in identity["packages"].items())
    if require_image_pins and not matches:
        raise ValueError("rendered tests did not use the sandbox image's exact package pins")
    hashes = {}
    for name in SOURCE_FILES:
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("browser acceptance source is missing or not a bounded regular file")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=10,
    ).strip()
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--", *SOURCE_FILES], cwd=root, text=True, timeout=10,
    ).strip()
    return {
        "schema_version": 1,
        "scope": "real-chromium-synthetic-pages-with-production-worker",
        "rendered_worker_passed": True,
        "sandbox_image_qualified": False,
        "xpra_qualified": False,
        "kernel_isolation_qualified": False,
        "multi_agent_qualified": False,
        "postgresql_controller_qualified": False,
        "dependency_pins_match_image": matches,
        "image_package_pins": pins,
        "python_version": platform.python_version(),
        "git_commit": commit,
        "tested_sources_dirty": bool(dirty),
        "source_sha256": hashes,
        "junit_sha256": hashlib.sha256(raw).hexdigest(),
        **identity,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--profile", choices=tuple(PROFILES), required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-image-pins", action="store_true")
    parser.add_argument("--require-engine-binary", action="store_true")
    args = parser.parse_args()
    try:
        result = verify(args.root, args.report, args.profile,
                        require_image_pins=args.require_image_pins,
                        require_engine_binary=args.require_engine_binary)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Never leave an older successful result after a failing recheck.
        args.output.unlink(missing_ok=True)
        parser.exit(1, f"browser rendered acceptance failed: {error}\n")
    print(f"{result['test_count']} rendered tests passed for {args.profile}; Xpra/OS unqualified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
