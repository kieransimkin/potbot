"""Strict, bounded guest messages. Identity and authority are deliberately absent."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from potbot.context.model import ROLES, validate_content
from potbot.domain.errors import InvalidInputError, ScopeDeniedError

SCHEMA = "potbot-context-rpc/v1"
MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_CHANNEL_BYTES = 8 * 1024 * 1024
MAX_CHANNEL_REQUESTS = 256


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidInputError("Duplicate context request field")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise InvalidInputError("Non-finite JSON is not accepted")


@dataclass(frozen=True, slots=True)
class ContextRequest:
    request_id: UUID
    method: str
    scope: str
    role: str
    content: str | None = None
    expected_revision: int | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if (not isinstance(self.request_id, UUID)
                or self.method not in ("context.read", "context.update")
                or self.scope not in ("agent", "sandbox") or self.role not in ROLES):
            raise InvalidInputError("Invalid context operation")
        if self.method == "context.read":
            if self.content is not None or self.expected_revision is not None or self.reason != "":
                raise InvalidInputError("Read request contains mutation fields")
            return
        if self.scope == "sandbox" and self.role == "AGENTS.md":
            raise ScopeDeniedError("Shared instructions require operator review")
        if not isinstance(self.content, str):
            raise InvalidInputError("Context update requires text")
        validate_content(self.content)
        if type(self.expected_revision) is not int or not 1 <= self.expected_revision < 2**63-1:
            raise InvalidInputError("A positive current revision is required")
        if not isinstance(self.reason, str) or len(self.reason) > 1000 or "\x00" in self.reason:
            raise InvalidInputError("Invalid context change reason")
        try:
            self.reason.encode("utf-8")
        except UnicodeError as exc:
            raise InvalidInputError("Invalid context change reason") from exc

    @property
    def fingerprint(self) -> str:
        material = [self.method, self.scope, self.role, self.content,
                    self.expected_revision, self.reason]
        raw = json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


def parse_request(data: bytes) -> ContextRequest:
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_FRAME_BYTES:
        raise InvalidInputError("Context frame exceeds its bound")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=_constant)
        if not isinstance(value, dict):
            raise InvalidInputError("Context request must be an object")
        common = {"schema", "request_id", "method", "scope", "role"}
        fields = common | {"content", "expected_revision", "reason"} if (
            value.get("method") == "context.update"
        ) else common
        if set(value) != fields or value["schema"] != SCHEMA:
            raise InvalidInputError("Unexpected context fields or schema")
        raw_id = value["request_id"]
        if not isinstance(raw_id, str) or len(raw_id) != 36:
            raise InvalidInputError("A canonical request UUID is required")
        request_id = UUID(raw_id)
        if str(request_id) != raw_id:
            raise InvalidInputError("A canonical request UUID is required")
        return ContextRequest(request_id, value["method"], value["scope"], value["role"],
                              value.get("content"), value.get("expected_revision"),
                              value.get("reason", ""))
    except (UnicodeError, ValueError, TypeError, KeyError, RecursionError) as exc:
        if isinstance(exc, InvalidInputError):
            raise
        raise InvalidInputError("Malformed context request") from None
