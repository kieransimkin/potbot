"""Private, content-addressed browser evidence on Potbot's existing ArtifactStore.

No new filesystem root, database or evidence ledger is introduced. Every image,
observation and manifest is published through ArtifactStore.put(actor, ...).
Execution Cog output/receipt persists the manifest reference in the normal Cog
work ledger. Content is sensitive and remains workspace/principal scoped.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import canonical_json, content_hash

_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MAX_JPEG = 600_000
_MAX_OBSERVATION = 250_000


@dataclass(frozen=True, slots=True)
class BrowserEvidenceScope:
    workspace_id: UUID
    principal_id: UUID
    session_id: str
    invocation_id: UUID
    policy_sha256: str

    def __post_init__(self) -> None:
        if (not all(isinstance(x, UUID) for x in (
                self.workspace_id, self.principal_id, self.invocation_id))
                or not isinstance(self.session_id, str) or not _ID.fullmatch(self.session_id)
                or not isinstance(self.policy_sha256, str) or not _SHA.fullmatch(self.policy_sha256)):
            raise InvalidInputError("browser evidence requires exact operator scope")


class BrowserEvidenceRecorder:
    def __init__(self, artifacts: Any, actor: Any) -> None:
        if artifacts is None or not callable(getattr(artifacts, "put", None)):
            raise ScopeDeniedError("browser evidence needs the installed artifact service")
        if not isinstance(getattr(actor, "workspace_id", None), UUID) or not isinstance(
            getattr(actor, "principal_id", None), UUID
        ):
            raise ScopeDeniedError("browser evidence needs an authenticated actor")
        self.artifacts, self.actor = artifacts, actor

    def _publish(self, raw: bytes, media_type: str) -> dict[str, str]:
        record = self.artifacts.put(self.actor, raw, media_type)
        if not isinstance(record, dict):
            raise StateMismatchError("artifact store returned an invalid receipt")
        expected = hashlib.sha256(raw).hexdigest()
        try:
            identity = str(UUID(str(record["id"])))
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise StateMismatchError("artifact store omitted its UUID") from error
        if record.get("digest") != expected:
            raise StateMismatchError("artifact store digest does not match published evidence")
        return {"id": identity, "sha256": expected}

    @staticmethod
    def _extract_image(result: dict[str, Any]) -> tuple[dict[str, Any], bytes | None]:
        """Remove base64 *before* serializing observation metadata or receipts."""
        projected = dict(result)
        frame = projected.get("frame")
        if not isinstance(frame, dict):
            if "base64" in projected:
                raise InvalidInputError("browser observation has an unexpected image field")
            return projected, None
        frame = dict(frame)
        token = frame.pop("base64", None)
        if frame.get("mime") != "image/jpeg" or not isinstance(frame.get("sha256"), str):
            raise InvalidInputError("browser evidence requires a JPEG observation")
        if not _SHA.fullmatch(frame["sha256"]) or type(frame.get("bytes")) is not int:
            raise InvalidInputError("browser image manifest is malformed")
        if not 0 <= frame["bytes"] <= _MAX_JPEG:
            raise InvalidInputError("browser image exceeds the capture limit")
        if token is None:
            projected["frame"] = frame
            return projected, None
        if not isinstance(token, str) or len(token) > (_MAX_JPEG * 4 // 3 + 8):
            raise InvalidInputError("browser image encoding exceeds its limit")
        try:
            data = base64.b64decode(token, validate=True)
        except (ValueError, binascii.Error) as error:
            raise InvalidInputError("browser image has invalid base64") from error
        if (len(data) != frame["bytes"] or hashlib.sha256(data).hexdigest() != frame["sha256"]
                or len(data) < 6 or not data.startswith(b"\xff\xd8\xff")
                or not data.endswith(b"\xff\xd9")):
            raise InvalidInputError("browser image failed integrity or JPEG checks")
        projected["frame"] = frame
        return projected, data

    def record(
        self, *, scope: BrowserEvidenceScope, command: dict[str, Any],
        result: dict[str, Any], kind: str, require_image: bool = False,
    ) -> dict[str, Any]:
        if (scope.workspace_id != self.actor.workspace_id
                or scope.principal_id != self.actor.principal_id):
            raise ScopeDeniedError("browser evidence cannot cross the authenticated workspace")
        if kind not in ("observation", "action", "capture") or not isinstance(
            command, dict
        ) or not isinstance(result, dict):
            raise InvalidInputError("invalid browser evidence kind")
        if require_image and (kind != "capture" or command.get("op") != "sample"):
            raise InvalidInputError("only an explicit sample may retain rendered bytes")
        projected, image = self._extract_image(result)
        if (kind != "capture" and image is not None) or (require_image and image is None):
            raise ScopeDeniedError("screen pixels must be captured by an explicit capture Cog")
        observed = canonical_json(projected).encode("utf-8")
        if not 0 < len(observed) <= _MAX_OBSERVATION:
            raise InvalidInputError("browser observation exceeds its private artifact limit")
        command_digest = content_hash(command)
        # No command body (possibly containing form values), authentication
        # header, query string or screenshot base64 goes into the manifest.
        picture = self._publish(image, "image/jpeg") if image is not None else None
        observation = self._publish(observed, "application/json")
        manifest_body = {
            "schema_version": 1,
            "type_ref": "syn://type/potbot-browser-evidence-v1",
            "workspace_id": str(scope.workspace_id),
            "principal_id": str(scope.principal_id),
            "session_id": scope.session_id,
            "invocation_id": str(scope.invocation_id),
            "policy_sha256": scope.policy_sha256,
            "kind": kind,
            "command_sha256": command_digest,
            "observation": observation,
            "image": picture,
            "captured_unix_ns": time.time_ns(),
            "sensitive": True,
            "redaction": "private-artifact-no-public-preview",
        }
        manifest = self._publish(canonical_json(manifest_body).encode("utf-8"), "application/json")
        # Bounded output suitable for the existing <=16 KiB SYN Cog transport.
        return {
            "schema_version": 1,
            "session_id": scope.session_id,
            "status": "recorded",
            "kind": kind,
            "command_sha256": command_digest,
            "observation_artifact_id": observation["id"],
            "observation_sha256": observation["sha256"],
            "image_artifact_id": picture["id"] if picture else None,
            "image_sha256": picture["sha256"] if picture else None,
            "manifest_artifact_id": manifest["id"],
            "manifest_sha256": manifest["sha256"],
        }
