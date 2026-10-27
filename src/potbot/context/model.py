"""Portable, unprivileged context document format and identity validation."""
from __future__ import annotations

import hashlib
import re
from uuid import UUID

from potbot.domain.errors import InvalidInputError

ROLES = ("AGENTS.md", "MODEL.md", "MEMORY.md")
KINDS = frozenset(("sandbox", "agent", "operator", "tool", "skill", "worker",
                   "workflow", "gateway", "model", "connector", "primitive"))
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
MAX_CONTENT_BYTES = 1_048_576


def validate_identity(kind: str, name: str, role: str | None = None) -> None:
    if kind not in KINDS or not isinstance(name, str) or not _NAME.fullmatch(name):
        raise InvalidInputError("Invalid context kind or object name.")
    if role is not None and role not in ROLES:
        raise InvalidInputError("Invalid context document role.")


def validate_content(content: str) -> None:
    if not isinstance(content, str) or "\x00" in content:
        raise InvalidInputError("Context must be valid UTF-8 text.")
    try:
        size = len(content.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise InvalidInputError("Context must be valid UTF-8 text.") from exc
    if size > MAX_CONTENT_BYTES:
        raise InvalidInputError("Context exceeds the document byte limit.")


def sha256(content: str) -> str:
    validate_content(content)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def document_templates(kind: str, name: str) -> dict[str, str]:
    validate_identity(kind, name)
    display = f"{kind}:{name}"
    return {
        "AGENTS.md": f"# Instructions: {display}\n\nNo object-specific instructions configured.\n",
        "MODEL.md": (f"# Working model: {display}\n\n## Observed state\n\nUnknown.\n"
                     "\n## Uncertainties\n\nDiscovery pending.\n"),
        "MEMORY.md": f"# History: {display}\n\nNo observations recorded.\n",
    }


def sandbox_object_id(workspace_id: UUID) -> UUID:
    """Stable identity, independent of external and user-chosen object names."""
    from uuid import NAMESPACE_URL, uuid5

    if not isinstance(workspace_id, UUID):
        raise InvalidInputError("Workspace identity must be a UUID.")
    return uuid5(NAMESPACE_URL, f"potbot:context:sandbox:{workspace_id}")
