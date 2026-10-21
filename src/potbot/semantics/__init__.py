"""SYN catalogs, versioned semantic releases and untrusted structural proposals.

Validation establishes shape and registered signatures only. It never grants
execution capabilities, resolves database ownership, or establishes truth.
"""

from potbot.semantics.registry import SemanticRegistry
from potbot.semantics.validation import SemanticValidator, ValidatedProposal, parse_scalar_literal

__all__ = ["SemanticRegistry", "SemanticValidator", "ValidatedProposal", "parse_scalar_literal"]
