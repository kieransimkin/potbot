"""Local Syn credential identities bind installation, workspace and service origin."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

from potbot.domain.errors import InvalidInputError

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_REFERENCE = re.compile(rf"secret://syn/({_UUID})/({_UUID})/([0-9a-f]{{64}})/communication-token\Z")
_VAULT_NAME = re.compile(rf"syn\.({_UUID})\.({_UUID})\.([0-9a-f]{{64}})\.communication_token\Z")


def service_origin(value: str) -> str:
    """Canonicalize only an HTTPS origin, or explicit loopback HTTP origin."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        loopback = host == "localhost"
        if host and not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if (
            not isinstance(value, str)
            or not value.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or not host
            or parsed.scheme not in {"https", "http"}
            or (parsed.scheme == "http" and not loopback)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "\\" in value
        ):
            raise ValueError
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        host = f"[{host}]" if ":" in host else host
        suffix = "" if port in {None, 443 if parsed.scheme == "https" else 80} else f":{port}"
        return f"{parsed.scheme}://{host}{suffix}"
    except (ValueError, TypeError, AttributeError):
        raise InvalidInputError(
            "Syn requires HTTPS or loopback HTTP with one explicit origin."
        ) from None


@dataclass(frozen=True, slots=True)
class SynCredentialScope:
    installation_id: UUID
    workspace_id: UUID
    recipient_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.installation_id, UUID)
            or not isinstance(self.workspace_id, UUID)
            or not isinstance(self.recipient_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.recipient_sha256) is None
        ):
            raise InvalidInputError(
                "Syn credential scope requires exact installation/workspace/origin identities."
            )

    @classmethod
    def for_service(cls, installation_id: UUID, workspace_id: UUID, url: str) -> SynCredentialScope:
        digest = hashlib.sha256(service_origin(url).encode("ascii")).hexdigest()
        return cls(installation_id, workspace_id, digest)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> SynCredentialScope:
        try:
            identities = [
                environment[name] for name in ("POTBOT_INSTALLATION_ID", "POTBOT_SYN_WORKSPACE_ID")
            ]
            installation, workspace = (UUID(value) for value in identities)
            if identities != [str(installation), str(workspace)]:
                raise ValueError
            return cls.for_service(installation, workspace, environment["POTBOT_SYN_URL"])
        except (ValueError, KeyError, TypeError, AttributeError):
            raise InvalidInputError(
                "Configure canonical POTBOT_INSTALLATION_ID and POTBOT_SYN_WORKSPACE_ID."
            ) from None

    @classmethod
    def from_reference(cls, reference: str) -> SynCredentialScope:
        match = _REFERENCE.fullmatch(reference)
        if match is None:
            raise InvalidInputError("Unknown Syn credential reference.")
        return cls(UUID(match[1]), UUID(match[2]), match[3])

    @property
    def reference(self) -> str:
        return f"secret://syn/{self.installation_id}/{self.workspace_id}/{self.recipient_sha256}/communication-token"

    @property
    def vault_name(self) -> str:
        return f"syn.{self.installation_id}.{self.workspace_id}.{self.recipient_sha256}.communication_token"


def is_syn_credential_name(name: str) -> bool:
    return isinstance(name, str) and _VAULT_NAME.fullmatch(name) is not None
