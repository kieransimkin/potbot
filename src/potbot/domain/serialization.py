"""Canonical, finite JSON representations for durable command and content identities."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from potbot.domain.errors import InvalidInputError


def _validate(value: Any) -> None:
    if value is None or type(value) in (str, bool, int, float):
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _validate(item)
        return
    raise InvalidInputError("Only JSON-compatible values with string object keys are accepted.")


def canonical_json(value: Any) -> str:
    """Serialize without implementation-specific object coercion or NaN values."""
    try:
        _validate(value)
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError, RecursionError):
        raise InvalidInputError("The value is not finite, canonical JSON.") from None


def content_hash(value: Any) -> str:
    """Return the lowercase SHA-256 digest of canonical UTF-8 JSON."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
