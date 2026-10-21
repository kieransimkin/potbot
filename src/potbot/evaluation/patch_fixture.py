"""Installed deterministic acceptance profile for the Phase 4 vertical slice."""

from __future__ import annotations

import difflib
import sys
from pathlib import Path

from potbot.domain.execution import ToolDefinition, ToolRegistry
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement
from potbot.domain.verification import CheckDefinition, VerificationProfile
from potbot.evaluation.repository_fixture import FAULTY_LINE, REPAIRED_LINE
from potbot.tools.patch_application import patch_tool_definition


def fixture_tools() -> ToolRegistry:
    tools = [
        patch_tool_definition(),
        ToolDefinition(
            "fixture.compile",
            "test",
            (
                sys.executable,
                "-I",
                "-B",
                "-c",
                "from pathlib import Path; p=Path('calculator.py'); compile(p.read_bytes(), str(p), 'exec'); print('compiled')",
            ),
            mutates=False,
            timeout_ms=5000,
        ),
    ]
    for name, test in (
        ("total", "tests.test_calculator.CalculatorTests.test_total"),
        ("empty", "tests.test_calculator.CalculatorTests.test_empty_total"),
        ("preexisting", "tests.test_preexisting.PreExistingFailure.test_unrelated_known_failure"),
    ):
        program = (
            "import sys,unittest; sys.path.insert(0,'.'); "
            f"suite=unittest.defaultTestLoader.loadTestsFromName({test!r}); "
            "result=unittest.TextTestRunner(verbosity=2).run(suite); sys.exit(not result.wasSuccessful())"
        )
        tools.append(
            ToolDefinition(
                "fixture." + name,
                "test",
                (sys.executable, "-I", "-B", "-c", program),
                mutates=False,
                timeout_ms=5000,
            )
        )
    return ToolRegistry(tuple(tools))


def fixture_profile(*, preexisting: bool = False) -> VerificationProfile:
    checks = [
        CheckDefinition("compile", "python.syntax", "fixture.compile"),
        CheckDefinition(
            "total", "calculator.total", "fixture.total", ("R1",), ("tests/test_calculator.py",)
        ),
        CheckDefinition(
            "empty", "calculator.empty", "fixture.empty", ("R2",), ("tests/test_calculator.py",)
        ),
    ]
    if preexisting:
        checks.append(
            CheckDefinition(
                "preexisting",
                "unrelated.known_failure",
                "fixture.preexisting",
                definition_paths=("tests/test_preexisting.py",),
                mandatory=False,
            )
        )
    return VerificationProfile("calculator-fixture", 1, tuple(checks))


def fixture_contract(commit: str) -> ContractSpec:
    return ContractSpec(
        "Repair the supplied calculator fixture without weakening its checks.",
        "git:" + commit,
        DeliverableType.PATCH,
        (
            Requirement("R1", "Correct nonempty total", "total([2, 3]) returns 5"),
            Requirement("R2", "Correct empty total", "total([]) returns 0"),
        ),
        ("calculator.py",),
        ("test", "patch"),
        "calculator-fixture-v1",
        (("tokens", 100000), ("calls", 100), ("tool_ms", 600000), ("concurrency", 4)),
    )


def fixture_patch(source: Path) -> bytes:
    before = (source / "calculator.py").read_text(encoding="utf-8")
    after = before.replace(FAULTY_LINE, REPAIRED_LINE, 1)
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="a/calculator.py",
            tofile="b/calculator.py",
        )
    ).encode("utf-8")
