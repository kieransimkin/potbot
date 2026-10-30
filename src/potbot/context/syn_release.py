"""Opt-in Syn 1.0.0 context-document type extension.

The historical 0.1.0 data-type catalog and its pinned SHA-256 remain immutable.
A caller must explicitly request this extension when publishing a new Syn
snapshot. Exact filenames indicate roles, never validity, policy, or trust.
"""
from __future__ import annotations

from potbot.context.semantic_roles import ROLE_TYPES
from potbot.domain.syn import SynSnapshot, SynType, SynnyRef
from potbot.semantics import catalog

_RELEASE = "potbot-context-document-roles/1.0.0"
_PARENTS = (catalog.data_type_ref("dt:file"),)
_DESCRIPTIONS = {
    "MODEL.md": "Checked exact basename MODEL.md; represents current context state, without trust or instruction authority.",
    "MEMORY.md": "Checked exact basename MEMORY.md; represents historical context, without trust or instruction authority.",
    "SKILL.md": "Checked exact basename SKILL.md; identifies a skill entry file, without trust or instruction authority.",
}


def context_syn_types() -> tuple[SynType, ...]:
    """Return exactly three explicit types; AGENTS.md already exists in Syn."""
    return tuple(
        SynType(SynnyRef(ROLE_TYPES[basename]), basename, _PARENTS, description, ())
        for basename, description in sorted(_DESCRIPTIONS.items())
    )


def context_syn_snapshot() -> SynSnapshot:
    """Generate the opt-in release for existing merge_types/DAG validation."""
    return SynSnapshot(_RELEASE, "extend_builtin", context_syn_types(), ())


def context_syn_catalog() -> tuple[SynType, ...]:
    """Validate that the extension integrates without altering pinned aliases."""
    return catalog.merge_types(context_syn_snapshot())
