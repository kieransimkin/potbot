"""Operator boundary: identity and authorization must come from trusted runtime."""
from dataclasses import dataclass
from .core import InvalidContext

@dataclass(frozen=True)
class Principal:
    subject: str
    permissions: frozenset[str]

class OperatorContextService:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _authorize(principal, permission):
        if not isinstance(principal, Principal) or not principal.subject:
            raise PermissionError("unauthenticated")
        if permission not in principal.permissions:
            raise PermissionError(f"missing permission: {permission}")

    def read(self, principal, kind, name, role):
        self._authorize(principal, "context:read")
        return self.store.read(kind,name,role)

    def update(self, principal, kind, name, role, content, expected_revision, reason=""):
        self._authorize(principal, "context:write")
        return self.store.write(kind,name,role,content,expected_revision,
                                principal.subject,reason)

    def provision_sandbox(self, principal, sandbox_id):
        self._authorize(principal, "context:provision")
        return self.store.provision("sandbox",sandbox_id,principal.subject)

    def history(self, principal, kind, name, role, limit=50):
        self._authorize(principal, "context:read")
        return self.store.history(kind,name,role,limit)
