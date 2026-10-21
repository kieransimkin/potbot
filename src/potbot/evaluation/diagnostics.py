"""Credential scrubbing for the deliberately narrow Phase 1 CI artifacts."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from html import escape
from urllib.parse import unquote, urlsplit

_SECRET_NAME = re.compile(r"(?:PASSWORD|SECRET|TOKEN|(?:^|_)KEY(?:$|_)|DSN)", re.IGNORECASE)
_DATABASE_URI = re.compile(r"postgres(?:ql)?://[^\s\"'<>]+", re.IGNORECASE)
_QUOTE = r"(?:[\"']|&quot;|&apos;|&#(?:34|39);|&#x(?:22|27);)"
_OWNERSHIP_TOKEN = re.compile(
    rf"(?P<prefix>{_QUOTE}?ownership_token{_QUOTE}?\s*[:=]\s*)"
    rf"(?P<quote>{_QUOTE}).*?(?P=quote)",
    re.IGNORECASE,
)


def sanitize_text(text: str, environment: Mapping[str, str] | None = None) -> str:
    """Remove known environment secrets, database URIs, and fixture capabilities.

    This is a bounded CI safeguard, not a general-purpose secret detector. Never
    collect arbitrary repository files or dump the environment as diagnostics.
    """
    environment = os.environ if environment is None else environment
    secrets: set[str] = set()
    for name, value in environment.items():
        if not _SECRET_NAME.search(name) or not value:
            continue
        if len(value) >= 4:
            secrets.add(value)
        if "DSN" in name.upper():
            try:
                password = urlsplit(value).password
            except ValueError:
                password = None
            if password:
                secrets.update({password, unquote(password)})
    # Pytest serializes captured output into both XML text and XML attributes.
    # Scrub those forms without decoding or otherwise corrupting the document.
    for value in tuple(secrets):
        escaped = escape(value, quote=False)
        secrets.update({escaped, escaped.replace('"', "&quot;"), escape(value, quote=True)})
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    text = _DATABASE_URI.sub("postgresql://[REDACTED]", text)
    return _OWNERSHIP_TOKEN.sub(
        lambda match: f"{match['prefix']}{match['quote']}[REDACTED]{match['quote']}",
        text,
    )
