from __future__ import annotations

import base64

import pytest

from potbot.domain.errors import ScopeDeniedError
from potbot.tools.secrets import CaptureRedactionError, CaptureRedactor, SecretReferenceProvider


def test_controller_secret_references_and_redacted_capture() -> None:
    secret = "canary confidential/credential+823"
    provider = SecretReferenceProvider({"vault/build": secret})
    material = provider.resolve(
        {"BUILD_CREDENTIAL": "vault/build"},
        permitted_references=frozenset({"vault/build"}),
        permitted_environment=frozenset({"BUILD_CREDENTIAL"}),
    )
    assert material.environment["BUILD_CREDENTIAL"] == secret
    assert secret not in repr(material)
    metadata = material.redactor.redact_metadata(
        {"command": ["builder", secret], "token": "opaque unrelated token", "update_token": 2}
    )
    assert metadata["update_token"] == 2
    assert metadata["token"] == "[REDACTED]"
    assert secret not in str(metadata)
    capture = secret.encode() + b" " + base64.b64encode(secret.encode())
    assert material.redactor.redact_bytes(capture) == b"[REDACTED] [REDACTED]"
    for allowed_refs, allowed_env in (
        (frozenset(), frozenset({"BUILD_CREDENTIAL"})),
        (frozenset({"vault/build"}), frozenset()),
    ):
        with pytest.raises(ScopeDeniedError):
            provider.resolve(
                {"BUILD_CREDENTIAL": "vault/build"},
                permitted_references=allowed_refs,
                permitted_environment=allowed_env,
            )


def test_capture_policy_fails_closed_and_incomplete_secret_is_hidden() -> None:
    redactor = CaptureRedactor(("SENSITIVE-CANARY",), reject_patterns=(b"unclassified-credential",))
    assert redactor.redact_bytes(b"prefix SENSITIVE-", complete=False) == b"prefix [REDACTED]"
    with pytest.raises(CaptureRedactionError):
        redactor.redact_bytes(b"unclassified-credential")
