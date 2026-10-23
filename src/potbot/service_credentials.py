"""Hosted API credentials resolved at use time, outside jobs and configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping

from potbot.credentials import CredentialStore, credential_profile
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError
from potbot.domain.syn_credentials import SynCredentialScope

PROVIDER_ENVIRONMENT = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "xai": "XAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "evolink": "EVOLINK_API_KEY",
}


class ServiceCredentials:
    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        store: CredentialStore | None = None,
    ) -> None:
        self.environment = os.environ if environment is None else environment
        self.store = store or CredentialStore(credential_profile(self.environment))

    def resolve(self, reference: str) -> str:
        if not isinstance(reference, str):
            raise InvalidInputError("Unknown service credential reference.")
        if reference == "secret://api/access-token":
            name, variable = "api.access_token", "POTBOT_API_TOKEN"
        elif reference.startswith("secret://providers/"):
            provider = reference.removeprefix("secret://providers/")
            if provider not in PROVIDER_ENVIRONMENT:
                raise InvalidInputError("Unknown provider credential reference.")
            name, variable = f"providers.{provider}", PROVIDER_ENVIRONMENT[provider]
        elif reference.startswith("secret://syn/"):
            requested = SynCredentialScope.from_reference(reference)
            configured = SynCredentialScope.from_environment(self.environment)
            if requested != configured:
                raise ScopeDeniedError("Syn credential is unavailable for this service scope.")
            name, variable = requested.vault_name, "POTBOT_SYN_TOKEN"
        else:
            raise InvalidInputError("Unknown service credential reference.")
        value = self.environment.get(variable)
        if not value:
            try:
                value = self.store.get(name)
            except CapabilityUnavailableError:
                raise CapabilityUnavailableError(
                    f"Unlock the native credential store or set {variable} explicitly."
                ) from None
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 4096
            or any(not 33 <= ord(char) <= 126 for char in value)
            or ((name == "api.access_token" or variable == "POTBOT_SYN_TOKEN") and len(value) < 32)
        ):
            raise CapabilityUnavailableError(
                f"A valid {variable} or corresponding native credential is required."
            )
        return value

    def configured(self, provider: str) -> bool:
        try:
            self.resolve(f"secret://providers/{provider}")
            return True
        except CapabilityUnavailableError:
            return False
