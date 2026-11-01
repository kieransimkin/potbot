"""Strict, bounded guest messages. Identity and authority are deliberately absent."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from potbot.context.model import ROLES, validate_content
from potbot.domain.errors import InvalidInputError

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
    skill_id: UUID | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.request_id, UUID)
                or self.method not in ("context.read", "context.update")
                or self.scope not in ("agent", "sandbox", "worker", "skill") or self.role not in ROLES):
            raise InvalidInputError("Invalid context operation")
        if (self.scope == "skill") != isinstance(self.skill_id, UUID):
            raise InvalidInputError("A skill scope requires exactly one canonical skill ID")
        if self.scope != "skill" and self.skill_id is not None:
            raise InvalidInputError("Unexpected skill selector")
        if self.method == "context.read":
            if self.content is not None or self.expected_revision is not None or self.reason != "":
                raise InvalidInputError("Read request contains mutation fields")
            return
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
        if self.skill_id is not None:
            material.append(str(self.skill_id))
        raw = json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


_SKILL_FIELDS = {
    "skills.create": {"name", "content", "reason"},
    "skills.save": {"skill_id", "content", "expected_revision", "reason"},
    "skills.publish": {"skill_id", "expected_revision", "reason"},
    "skills.archive": {"skill_id", "expected_revision", "reason"},
    "skills.list": {"limit", "after"},
    "skills.load": {"skill_id", "release_sha256"},
}
_SKILL_MUTATIONS = frozenset(("skills.create", "skills.save", "skills.publish", "skills.archive"))


def canonical_uuid(value: object) -> UUID:
    if not isinstance(value, str) or len(value) != 36:
        raise InvalidInputError("A canonical UUID is required")
    try:
        result = UUID(value)
    except ValueError:
        raise InvalidInputError("A canonical UUID is required") from None
    if str(result) != value:
        raise InvalidInputError("A canonical UUID is required")
    return result


@dataclass(frozen=True, slots=True)
class SkillRequest:
    """Names select owned namespaces; IDs select records, never confer authority."""

    request_id: UUID
    method: str
    name: str | None = None
    skill_id: UUID | None = None
    content: str | None = None
    expected_revision: int | None = None
    release_sha256: str | None = None
    limit: int = 25
    after: UUID | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if (not isinstance(self.request_id, UUID) or not isinstance(self.method, str)
                or self.method not in _SKILL_FIELDS):
            raise InvalidInputError("Invalid skill operation")
        supplied = {
            key for key, value in (
                ("name", self.name), ("skill_id", self.skill_id), ("content", self.content),
                ("expected_revision", self.expected_revision), ("release_sha256", self.release_sha256),
                ("after", self.after),
            ) if value is not None
        }
        allowed = _SKILL_FIELDS[self.method]
        if supplied - allowed or (self.method != "skills.list" and self.limit != 25):
            raise InvalidInputError("Unexpected skill request fields")
        if "skill_id" in allowed and not isinstance(self.skill_id, UUID):
            raise InvalidInputError("Skill ID must be a UUID")
        if "name" in allowed and (not isinstance(self.name, str)
                or not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", self.name)):
            raise InvalidInputError("Invalid skill name")
        if "content" in allowed:
            if not isinstance(self.content, str):
                raise InvalidInputError("Skill content is required")
            validate_content(self.content)
        if "expected_revision" in allowed and (type(self.expected_revision) is not int
                or not 1 <= self.expected_revision < 2**63-1):
            raise InvalidInputError("Skill edit requires a current revision")
        if "release_sha256" in allowed and (not isinstance(self.release_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", self.release_sha256)):
            raise InvalidInputError("Load requires an exact published release digest")
        if self.method == "skills.list" and (type(self.limit) is not int or not 1 <= self.limit <= 50
                or (self.after is not None and not isinstance(self.after, UUID))):
            raise InvalidInputError("Invalid skill discovery bounds")
        if (not isinstance(self.reason, str) or len(self.reason) > 1000 or "\x00" in self.reason
                or (not self.mutates and self.reason != "")):
            raise InvalidInputError("Invalid skill change reason")
        try:
            self.reason.encode("utf-8")
        except UnicodeError:
            raise InvalidInputError("Invalid skill change reason") from None

    @property
    def mutates(self) -> bool:
        return self.method in _SKILL_MUTATIONS

    @property
    def fingerprint(self) -> str:
        material = [self.method, self.name, str(self.skill_id) if self.skill_id else None,
                    self.content, self.expected_revision, self.release_sha256,
                    self.limit, str(self.after) if self.after else None, self.reason]
        raw = json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


OperationRequest = ContextRequest | SkillRequest


def parse_request(data: bytes) -> OperationRequest:
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_FRAME_BYTES:
        raise InvalidInputError("Context frame exceeds its bound")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        if not isinstance(value, dict) or value.get("schema") != SCHEMA:
            raise InvalidInputError("Unexpected context schema")
        request_id = canonical_uuid(value.get("request_id"))
        method = value.get("method")
        if not isinstance(method, str):
            raise InvalidInputError("Invalid context method")
        if method in _SKILL_FIELDS:
            if set(value) != {"schema", "request_id", "method"} | _SKILL_FIELDS[method]:
                raise InvalidInputError("Unexpected skill fields")
            args = {key: value[key] for key in _SKILL_FIELDS[method]}
            for key in ("skill_id", "after"):
                if args.get(key) is not None:
                    args[key] = canonical_uuid(args[key])
            return SkillRequest(request_id, method, **args)
        common = {"schema", "request_id", "method", "scope", "role"}
        if value.get("scope") == "skill":
            common.add("skill_id")
        fields = common | {"content", "expected_revision", "reason"} if (
            method == "context.update"
        ) else common
        if set(value) != fields:
            raise InvalidInputError("Unexpected context fields")
        return ContextRequest(request_id, method, value["scope"], value["role"],
            value.get("content"), value.get("expected_revision"), value.get("reason", ""),
            canonical_uuid(value["skill_id"]) if "skill_id" in value else None)
    except (UnicodeError, ValueError, TypeError, KeyError, RecursionError) as exc:
        if isinstance(exc, InvalidInputError):
            raise
        raise InvalidInputError("Malformed context request") from None
