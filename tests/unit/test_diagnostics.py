"""Logs must remain useful without disclosing test connection capabilities."""

import json
from xml.etree import ElementTree

import pytest

from potbot.evaluation.diagnostics import sanitize_text


def test_scrubs_dsn_encoded_password_and_ownership_token() -> None:
    dsn = "postgresql://potbot_test:s%40fe-password@127.0.0.1:55432/potbot_test"
    log = (
        f"Connecting {dsn}\npassword=s@fe-password and s%40fe-password\n"
        '{"ownership_token": "a-secret-cleanup-capability", "status": "failed"}\n'
        "SQLSTATE 08001; migration bootstrap_v1"
    )
    sanitized = sanitize_text(log, {"POTBOT_TEST_DSN": dsn})
    for secret in (dsn, "s@fe-password", "s%40fe-password", "a-secret-cleanup-capability"):
        assert secret not in sanitized
    assert "SQLSTATE 08001; migration bootstrap_v1" in sanitized


def test_scrubs_unknown_database_uri_and_known_ci_secret() -> None:
    sanitized = sanitize_text(
        "postgres://user:unknown@localhost:5432/test\nCI_TOKEN: abcdefgh\npytest: 3 passed",
        {"CI_TOKEN": "abcdefgh"},
    )
    assert "unknown" not in sanitized
    assert "abcdefgh" not in sanitized
    assert "pytest: 3 passed" in sanitized


def test_ordinary_environment_values_do_not_destroy_diagnostics() -> None:
    assert sanitize_text("test failed", {"PATH": "test", "SHORT_TOKEN": "x"}) == "test failed"


@pytest.mark.parametrize(
    "representation",
    [
        "{'ownership_token': 'cleanup-secret'}",
        "RepositoryFixture(ownership_token='cleanup-secret', variant='baseline')",
        "&quot;ownership_token&quot;: &quot;cleanup-secret&quot;",
        "&#x27;ownership_token&#x27;: &#x27;cleanup-secret&#x27;",
    ],
)
def test_scrubs_fixture_capabilities_from_python_and_xml(representation: str) -> None:
    sanitized = sanitize_text(representation, {})
    assert "cleanup-secret" not in sanitized
    assert "ownership_token" in sanitized
    assert "[REDACTED]" in sanitized


def test_junit_redaction_preserves_xml_and_scrubs_decoded_attributes_and_text() -> None:
    secret = "s<cr&et\"with'quotes"
    capability = "b" * 64
    message = f"password={secret}; actual={ {'ownership_token': capability} }"
    root = ElementTree.Element("testsuites")
    suite = ElementTree.SubElement(root, "testsuite", name="postgres")
    case = ElementTree.SubElement(suite, "testcase", name="test_cleanup")
    failure = ElementTree.SubElement(case, "failure", message=message)
    failure.text = json.dumps({"ownership_token": capability}) + f"\npassword={secret}"
    original = ElementTree.tostring(root, encoding="unicode")

    sanitized = sanitize_text(original, {"TEST_PASSWORD": secret})
    parsed = ElementTree.fromstring(sanitized)
    decoded = "\n".join(
        [*parsed.itertext(), *(value for node in parsed.iter() for value in node.attrib.values())]
    )
    assert secret not in decoded
    assert capability not in decoded
    assert "[REDACTED]" in decoded
    assert parsed.find("./testsuite/testcase").attrib["name"] == "test_cleanup"
