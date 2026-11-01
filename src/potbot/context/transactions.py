"""Transaction-only interface shared by context stores and the session broker."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol

from potbot.domain.auth import WorkspaceContext


class ContextDatabase(Protocol):
    def transaction(self, context: WorkspaceContext) -> AbstractContextManager[Any]: ...
