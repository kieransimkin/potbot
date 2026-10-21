"""Trusted request identity, separate from model- or user-supplied task fields."""

from dataclasses import dataclass
from uuid import UUID

from potbot.domain.errors import InvalidInputError


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    """Identity authenticated by an adapter and rechecked by the database.

    Constructing this value does not grant authority. The transaction boundary
    verifies the principal against the PostgreSQL login and workspace membership.
    """

    workspace_id: UUID
    principal_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, UUID) or not isinstance(self.principal_id, UUID):
            raise InvalidInputError("Workspace and principal identities must be UUIDs.")
