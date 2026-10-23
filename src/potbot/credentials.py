"""Native credential storage and first-use PostgreSQL connection resolution.

Only OS credential backends are selected. No file, null or plugin fallback is
accepted, and database URLs are never returned by the management CLI. Explicit
environment values remain useful for CI/headless hosts and are never autosaved.
"""

from __future__ import annotations

import argparse
import getpass
import hmac
import importlib
import json
import os
import re
import sys
import warnings
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, Protocol, TextIO, cast
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID

from potbot.domain.errors import (
    CapabilityUnavailableError,
    DomainError,
    InvalidInputError,
    SourceUnavailableError,
)
from potbot.domain.syn_credentials import SynCredentialScope, is_syn_credential_name

Purpose = Literal["application", "admin"]
SERVICE_NAME = "DanceFlow.Potbot"
PROFILE_ENV = "POTBOT_CREDENTIAL_PROFILE"
_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
SERVICE_CREDENTIAL_NAMES = (
    "providers.anthropic",
    "providers.openai",
    "providers.xai",
    "providers.gemini",
    "providers.evolink",
    "api.access_token",
)
_NAMES = {"postgres.application", "postgres.admin", *SERVICE_CREDENTIAL_NAMES}
_PURPOSE_ENV = {"application": "POTBOT_DSN", "admin": "POTBOT_ADMIN_DSN"}
_URL_ERROR = "Enter a PostgreSQL URL for one explicit host and database; its value is not logged."
_CONNECTION_ERROR = (
    "Could not verify the PostgreSQL connection. Check the address, credentials, TLS "
    "settings and server availability, then retry. No URL or password was logged."
)
_STORE_ERROR = (
    "The OS credential store is unavailable or locked. Unlock the native credential vault "
    "(Windows Credential Manager, macOS Keychain, or Linux Secret Service), or provide "
    "POTBOT_DSN/POTBOT_ADMIN_DSN explicitly for a headless run. No plaintext fallback is used."
)


class KeyringBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


def native_backend(platform_name: str | None = None) -> KeyringBackend:
    """Select a bundled native adapter without loading configurable plugin chains."""
    platform_name = sys.platform if platform_name is None else platform_name
    choices = {
        "win32": ("keyring.backends.Windows", "WinVaultKeyring"),
        "darwin": ("keyring.backends.macOS", "Keyring"),
        "linux": ("keyring.backends.SecretService", "Keyring"),
    }
    selected = choices.get(platform_name)
    if selected is None:
        raise CapabilityUnavailableError(_STORE_ERROR)
    try:
        module = importlib.import_module(selected[0])
        backend = getattr(module, selected[1])()
        if backend.priority <= 0:
            raise RuntimeError("Native backend is disabled")
        return cast(KeyringBackend, backend)
    except Exception:
        # Backend errors can contain service names, decrypted values or paths.
        raise CapabilityUnavailableError(_STORE_ERROR) from None


def credential_profile(environment: Mapping[str, str] | None = None) -> str:
    environment = os.environ if environment is None else environment
    profile = environment.get(PROFILE_ENV, "default")
    if not isinstance(profile, str) or not _PROFILE.fullmatch(profile):
        raise InvalidInputError(
            "Credential profile must be a bounded letters/digits/dot/dash name."
        )
    return profile


class CredentialStore:
    """Profile-scoped values held only by the operating system's credential vault.

    Backend injection supports deterministic contract tests; application startup
    always uses the explicit native selector above.
    """

    def __init__(self, profile: str = "default", *, backend: KeyringBackend | None = None) -> None:
        self.profile = credential_profile({PROFILE_ENV: profile})
        self._provided_backend = backend

    def _backend(self) -> KeyringBackend:
        if self._provided_backend is None:
            self._provided_backend = native_backend()
        return self._provided_backend

    def _account(self, name: str) -> str:
        if name not in _NAMES and not is_syn_credential_name(name):
            raise InvalidInputError("Unknown credential name.")
        return f"{self.profile}:{name}"

    def get(self, name: str) -> str | None:
        account = self._account(name)
        try:
            value = self._backend().get_password(SERVICE_NAME, account)
        except Exception:
            raise CapabilityUnavailableError(_STORE_ERROR) from None
        if value is not None and (not isinstance(value, str) or not value or "\x00" in value):
            raise CapabilityUnavailableError(
                "The stored credential is invalid; replace it explicitly."
            )
        return value

    def set(self, name: str, value: str) -> None:
        account = self._account(name)
        if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
            raise InvalidInputError("Credential value is empty or outside the supported bound.")
        try:
            backend = self._backend()
            backend.set_password(SERVICE_NAME, account, value)
            persisted = backend.get_password(SERVICE_NAME, account)
            if not isinstance(persisted, str) or not hmac.compare_digest(
                value.encode(), persisted.encode()
            ):
                raise RuntimeError("Credential persistence was not confirmed")
        except Exception:
            raise CapabilityUnavailableError(
                "Credential persistence was not confirmed by the OS vault. Startup did not save "
                "a plaintext copy; unlock or repair the vault and retry."
            ) from None

    def delete(self, name: str) -> bool:
        account = self._account(name)
        if self.get(name) is None:
            return False
        try:
            backend = self._backend()
            backend.delete_password(SERVICE_NAME, account)
            if backend.get_password(SERVICE_NAME, account) is not None:
                raise RuntimeError("Credential removal was not confirmed")
        except Exception:
            raise CapabilityUnavailableError(
                "Credential removal was not confirmed by the OS vault."
            ) from None
        return True

    def status(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "storage": "native_os_credential_vault",
            "application_configured": self.get("postgres.application") is not None,
            "admin_configured": self.get("postgres.admin") is not None,
        }


def validate_postgres_url(value: str) -> str:
    """Bound new persisted inputs to one explicit endpoint; never include them in errors."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise InvalidInputError(_URL_ERROR)
    try:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        if (
            parsed.scheme not in {"postgres", "postgresql"}
            or not parsed.hostname
            or "," in unquote(parsed.hostname)
            or not parsed.path.startswith("/")
            or len(parsed.path) <= 1
            or "/" in parsed.path[1:]
            or parsed.fragment
            or any(len(values) != 1 for values in query.values())
            or any("," in query.get(name, [""])[0] for name in ("host", "hostaddr", "port"))
        ):
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
    except (ValueError, TypeError):
        raise InvalidInputError(_URL_ERROR) from None
    return value


def validate_connection(dsn: str) -> None:
    """Check connectivity read-only, with bounded connect and statement timeouts.

    A connection check does not migrate, provision roles, or grant authorization.
    Prompted/stored URLs have one endpoint so libpq does not multiply the connect
    timeout over an arbitrary host list.
    """
    try:
        import psycopg

        with psycopg.connect(
            dsn,
            connect_timeout=5,
            application_name="potbot-startup-check",
            options="-c default_transaction_read_only=on -c statement_timeout=3000 -c lock_timeout=3000",
        ) as connection:
            if connection.execute("SELECT 1").fetchone() != (1,):
                raise RuntimeError("Unexpected database response")
    except ImportError:
        raise CapabilityUnavailableError(
            "PostgreSQL support is missing; run the uv startup launcher."
        ) from None
    except Exception:
        raise SourceUnavailableError(_CONNECTION_ERROR) from None


def _interactive_allowed(interactive: bool | None, environment: Mapping[str, str]) -> bool:
    if interactive is not None:
        return interactive
    if environment.get("POTBOT_NON_INTERACTIVE", "").lower() in {"1", "true", "yes"}:
        return False
    return sys.stdin.isatty() and sys.stderr.isatty()


def _hidden_prompt(label: str) -> str:
    try:
        with warnings.catch_warnings():
            # getpass otherwise falls back to echoed input on an unsuitable TTY.
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(label)
    except (getpass.GetPassWarning, EOFError, OSError):
        raise InvalidInputError(
            "A hidden credential prompt requires an interactive terminal."
        ) from None
    except KeyboardInterrupt:
        raise InvalidInputError("Credential entry was cancelled.") from None


def prompt_and_store(
    store: CredentialStore,
    purpose: Purpose = "application",
    *,
    interactive: bool | None = None,
    environment: Mapping[str, str] | None = None,
    prompt: Callable[[str], str] | None = None,
    validator: Callable[[str], None] | None = None,
    output: TextIO | None = None,
) -> str:
    environment = os.environ if environment is None else environment
    if purpose not in _PURPOSE_ENV:
        raise InvalidInputError("Unknown PostgreSQL credential purpose.")
    if not _interactive_allowed(interactive, environment):
        raise InvalidInputError(
            f"PostgreSQL credentials are missing. Set {_PURPOSE_ENV[purpose]} for this run, "
            "or run startup/credentials set in an interactive terminal."
        )
    # Confirm the vault can be opened before asking the user to enter a secret.
    store.get(f"postgres.{purpose}")
    stream = sys.stderr if output is None else output
    print(
        f"Configure the PostgreSQL {purpose} connection for profile {store.profile}.", file=stream
    )
    print(
        "Enter its connection URL. Input is hidden and will be saved in the OS credential vault.",
        file=stream,
    )
    value = validate_postgres_url((prompt or _hidden_prompt)(f"PostgreSQL {purpose} URL: "))
    if validator is None:
        validate_connection(value)
    else:
        try:
            validator(value)
        except Exception:
            # Even custom checks must not place a driver's raw diagnostic or URL
            # in the command's structured error output.
            raise SourceUnavailableError(_CONNECTION_ERROR) from None
    store.set(f"postgres.{purpose}", value)
    print("PostgreSQL connection verified and saved in the OS credential vault.", file=stream)
    return value


def resolve_postgres_dsn(
    purpose: Purpose = "application",
    *,
    environment: Mapping[str, str] | None = None,
    store: CredentialStore | None = None,
    interactive: bool | None = None,
    prompt: Callable[[str], str] | None = None,
    validator: Callable[[str], None] | None = None,
    output: TextIO | None = None,
) -> str:
    """Explicit environment, then native vault, then a verified hidden prompt."""
    environment = os.environ if environment is None else environment
    if purpose not in _PURPOSE_ENV:
        raise InvalidInputError("Unknown PostgreSQL credential purpose.")
    explicit = environment.get(_PURPOSE_ENV[purpose])
    if explicit:
        # Preserve existing libpq keyword DSNs and caller-controlled connection
        # parameters; environment values are an explicit, nonpersistent override.
        return explicit
    store = store or CredentialStore(credential_profile(environment))
    try:
        existing = store.get(f"postgres.{purpose}")
    except CapabilityUnavailableError:
        if not _interactive_allowed(interactive, environment):
            raise InvalidInputError(
                f"PostgreSQL credentials are unavailable in this headless session. Set "
                f"{_PURPOSE_ENV[purpose]}, or unlock a native credential vault."
            ) from None
        raise
    if existing is not None:
        return validate_postgres_url(existing)
    return prompt_and_store(
        store,
        purpose,
        interactive=interactive,
        environment=environment,
        prompt=prompt,
        validator=validator,
        output=output,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot credentials", description=__doc__)
    parser.add_argument("--profile", default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="show whether native credentials exist, never their values")
    commands.add_parser("service-status", help="inspect hosted API credential presence")
    for name in ("service-set", "service-delete"):
        command = commands.add_parser(name)
        command.add_argument("--name", required=True, choices=SERVICE_CREDENTIAL_NAMES)
    for name in ("syn-set", "syn-delete", "syn-status"):
        command = commands.add_parser(name, help="manage one scoped Syn communication credential")
        command.add_argument("--installation-id", required=True, type=UUID)
        command.add_argument("--workspace-id", required=True, type=UUID)
        command.add_argument("--url", required=True)
    for name in ("set", "delete"):
        command = commands.add_parser(name)
        command.add_argument("--purpose", choices=tuple(_PURPOSE_ENV), default="application")
    args = parser.parse_args(argv)
    try:
        store = CredentialStore(args.profile if args.profile is not None else credential_profile())
        result: dict[str, Any]
        if args.command.startswith("syn-"):
            scope = SynCredentialScope.for_service(
                args.installation_id, args.workspace_id, args.url
            )
            result = {"profile": store.profile, "credential_ref": scope.reference}
            if args.command == "syn-set":
                value = _hidden_prompt("Syn communication token (hidden): ")
                if not 32 <= len(value) <= 4096 or any(
                    not 33 <= ord(char) <= 126 for char in value
                ):
                    raise InvalidInputError("Syn tokens need 32..4096 visible ASCII characters.")
                store.set(scope.vault_name, value)
                result["saved"] = True
            elif args.command == "syn-delete":
                result["deleted"] = store.delete(scope.vault_name)
            else:
                result["configured"] = store.get(scope.vault_name) is not None
        elif args.command == "status":
            result = store.status()
        elif args.command == "service-status":
            result = {
                "profile": store.profile,
                "configured": {
                    name: store.get(name) is not None for name in SERVICE_CREDENTIAL_NAMES
                },
            }
        elif args.command == "service-set":
            value = _hidden_prompt(f"Value for {args.name} (hidden): ")
            if any(not 33 <= ord(char) <= 126 for char in value):
                raise InvalidInputError(
                    "API credentials must contain only visible ASCII characters."
                )
            if args.name == "api.access_token" and len(value) < 32:
                raise InvalidInputError("API access tokens need at least 32 characters.")
            store.set(args.name, value)
            result = {"profile": store.profile, "name": args.name, "saved": True}
        elif args.command == "service-delete":
            result = {
                "profile": store.profile,
                "name": args.name,
                "deleted": store.delete(args.name),
            }
        elif args.command == "set":
            prompt_and_store(store, args.purpose)
            result = {"profile": store.profile, "purpose": args.purpose, "saved": True}
        else:
            result = {
                "profile": store.profile,
                "purpose": args.purpose,
                "deleted": store.delete(f"postgres.{args.purpose}"),
            }
        print(json.dumps({"schema_version": 1, "data": result}, sort_keys=True))
        return 0
    except DomainError as error:
        print(
            json.dumps({"schema_version": 1, "error": error.to_dict()}, sort_keys=True),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
