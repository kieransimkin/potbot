"""Controller-owned secret references and bounded capture redaction.

This boundary covers literal UTF-8, URL-encoded and base64 forms of supplied
secrets. It cannot recognize arbitrary transformations performed by a hostile
program. Authorized secret-bearing operations therefore remain trusted tools.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, quote_plus

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, ToolFailureError

_ENVIRONMENT_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_SENSITIVE_FIELDS = frozenset(
    {"password", "secret", "token", "access_token", "refresh_token", "api_key", "authorization"}
)
_MARKER = b"[REDACTED]"


class CaptureRedactionError(ToolFailureError):
    """No capture may be published when the configured policy fails."""


@dataclass(frozen=True, slots=True)
class CaptureRedactor:
    secrets: tuple[str, ...] = field(default=(), repr=False)
    reject_patterns: tuple[bytes, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if len(self.secrets) > 128 or any(
            not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= 16384
            for value in self.secrets
        ):
            raise InvalidInputError("secret redaction inputs exceed the supported bound")
        if any(not isinstance(value, bytes) or not value for value in self.reject_patterns):
            raise InvalidInputError("capture rejection patterns must be nonempty bytes")

    @property
    def patterns(self) -> tuple[bytes, ...]:
        values: set[bytes] = set()
        for secret in self.secrets:
            raw = secret.encode("utf-8")
            values.update(
                (
                    raw,
                    quote(secret, safe="").encode(),
                    quote_plus(secret, safe="").encode(),
                    base64.b64encode(raw),
                    base64.urlsafe_b64encode(raw),
                )
            )
        return tuple(sorted(values, key=len, reverse=True))

    @property
    def overlap_bytes(self) -> int:
        return max((len(value) for value in self.patterns), default=1)

    def redact_bytes(self, data: bytes, *, complete: bool = True) -> bytes:
        if not isinstance(data, bytes):
            raise CaptureRedactionError("capture must be a byte sequence")
        result = data
        # Longest matches first prevent overlapping secret prefixes leaking.
        for pattern in self.patterns:
            result = result.replace(pattern, _MARKER)
            if not complete:
                # An interrupted or bounded capture may end inside a secret.
                for size in range(min(len(pattern) - 1, len(result)), 0, -1):
                    if result.endswith(pattern[:size]):
                        result = result[:-size] + _MARKER
                        break
        if any(pattern in result for pattern in self.reject_patterns):
            raise CaptureRedactionError("capture failed its configured publication policy")
        return result

    def redact_metadata(self, value: Any) -> Any:
        """Return a redacted copy; never modify an authority/configuration input."""
        if isinstance(value, str):
            return self.redact_bytes(value.encode("utf-8")).decode("utf-8")
        if isinstance(value, Mapping):
            return {
                str(key): (
                    _MARKER.decode()
                    if str(key).casefold() in _SENSITIVE_FIELDS
                    else self.redact_metadata(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.redact_metadata(item) for item in value]
        if value is None or type(value) in (bool, int, float):
            return value
        raise CaptureRedactionError("capture metadata contains an unsupported value")


@dataclass(frozen=True, slots=True)
class SecretMaterial:
    environment: Mapping[str, str] = field(repr=False)
    redactor: CaptureRedactor = field(repr=False)
    references: tuple[str, ...]


class SecretReferenceProvider:
    """Resolve references only after controller authorization of the operation."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def resolve(
        self,
        requested: Mapping[str, str],
        *,
        permitted_references: frozenset[str],
        permitted_environment: frozenset[str],
    ) -> SecretMaterial:
        if len(requested) > 128 or any(
            not isinstance(name, str)
            or not _ENVIRONMENT_NAME.fullmatch(name)
            or name not in permitted_environment
            or reference not in permitted_references
            or reference not in self._values
            for name, reference in requested.items()
        ):
            raise ScopeDeniedError("secret reference is unavailable for this operation")
        environment = {name: self._values[reference] for name, reference in requested.items()}
        if any(
            not isinstance(value, str) or "\x00" in value or not value
            for value in environment.values()
        ):
            raise InvalidInputError("secret value cannot be injected into the environment")
        return SecretMaterial(
            environment,
            CaptureRedactor(tuple(environment.values())),
            tuple(sorted(set(requested.values()))),
        )
