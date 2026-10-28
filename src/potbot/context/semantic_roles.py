"""Versioned, exact-case context filename roles proposed for the Syn registry.

This is a *candidate extension*, not a modification to the immutable 0.1.0
45-type registry or its published native SYN snapshots. Classification of a
basename never infers Markdown validity, trusted content, or instruction rank.
"""
from __future__ import annotations

from dataclasses import dataclass

from potbot.domain.errors import InvalidInputError

ROLE_TYPES = {
    "AGENTS.md": "synny://type/potbot-data-file-agents-md",  # existing published alias
    "MODEL.md": "synny://type/potbot-context-file-model-md-v1",
    "MEMORY.md": "synny://type/potbot-context-file-memory-md-v1",
    "SKILL.md": "synny://type/potbot-context-file-skill-md-v1",
}


@dataclass(frozen=True, slots=True)
class FilenameRole:
    basename: str
    type_ref: str
    classifier: str
    trusted: bool = False
    instructions_authoritative: bool = False


def classify_context_basename(basename: str) -> FilenameRole | None:
    """Classify a checked basename, not an arbitrary filesystem path."""
    if (not isinstance(basename, str) or not basename or "/" in basename
            or "\\" in basename or "\x00" in basename
            or basename in {".", ".."}):
        raise InvalidInputError("Expected an exact checked file basename")
    ref = ROLE_TYPES.get(basename)
    if ref is None:
        return None
    classifier = ("filename-agents-md-exact-v1" if basename == "AGENTS.md"
                  else f"filename-{basename.lower().replace('.', '-')}-exact-v1")
    return FilenameRole(basename, ref, classifier)
