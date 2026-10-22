"""Typed SYN Cog transformations. Importing this package executes no Cogs."""

from .contracts import CodingScope, CogOutcome, CogReceipt, CogRunContext, CogSignature, TypedCogValue
from .registry import (
    BoundCogRegistry,
    COG_COMPLETION_JSON,
    COG_JSON_ACTION,
    COG_JSON_CODING,
    COG_SCOPE_CODING,
    builtin_cog_snapshot,
)

__all__ = [
    "BoundCogRegistry",
    "COG_COMPLETION_JSON",
    "COG_JSON_ACTION",
    "COG_JSON_CODING",
    "COG_SCOPE_CODING",
    "CodingScope",
    "CogOutcome",
    "CogReceipt",
    "CogRunContext",
    "CogSignature",
    "TypedCogValue",
    "builtin_cog_snapshot",
]
