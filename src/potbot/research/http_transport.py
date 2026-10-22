"""Constrained HTTPS fetch for configured research providers, not arbitrary URLs.

Real deployments must also enforce outbound host/IP policy in their network
namespace/proxy: checking an HTTPS hostname cannot defend DNS rebinding alone.
Redirects are *never* followed by this transport, even to a permitted host.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import UUID

from potbot.domain.errors import InvalidInputError

if TYPE_CHECKING:
    import httpx


class FetchStatus(StrEnum):
    FETCHED = "fetched"
    REDIRECT_DENIED = "redirect_denied"
    RESPONSE_TOO_LARGE = "response_too_large"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class FetchPermit:
    """Exactly one operator-configured external resource and task reservation."""

    exact_url: str
    reservation_id: UUID
    action_id: UUID
    disclosure_decision_id: UUID
    max_bytes: int = 262144
    deadline_seconds: float = 8.0

    def __post_init__(self) -> None:
        for field in ("reservation_id", "action_id", "disclosure_decision_id"):
            if not isinstance(getattr(self, field), UUID):
                raise InvalidInputError(f"fetch {field} requires an authorized UUID")
        if not isinstance(self.exact_url, str) or len(self.exact_url) > 2048:
            raise InvalidInputError("provider URL is invalid")
        try:
            parts = urlsplit(self.exact_url)
            hostname = parts.hostname
            port = parts.port
        except ValueError:
            raise InvalidInputError("provider URL is invalid") from None
        if (
            parts.scheme != "https" or not hostname or not hostname.isascii()
            or "." not in hostname or parts.username is not None
            or parts.password is not None or parts.fragment or parts.query
            or (port is not None and port != 443)
            or not parts.path.startswith("/")
        ):
            raise InvalidInputError(
                "provider requires a fixed public HTTPS hostname without credentials or query"
            )
        if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost"):
            raise InvalidInputError("provider hostname is not permitted")
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise InvalidInputError("IP literals cannot be research-provider destinations")
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 1048576:
            raise InvalidInputError("invalid fetch byte limit")
        if isinstance(self.deadline_seconds, bool) or not isinstance(
            self.deadline_seconds, int | float
        ) or not math.isfinite(self.deadline_seconds) or not 0 < self.deadline_seconds <= 30:
            raise InvalidInputError("invalid fetch deadline")


@dataclass(frozen=True, slots=True)
class FetchResult:
    status: FetchStatus
    action_id: UUID
    request_url: str
    http_status: int | None
    data: bytes | None
    sha256: str | None
    received_bytes: int

    def __post_init__(self) -> None:
        if self.data is not None and (
            self.status != FetchStatus.FETCHED
            or self.sha256 != hashlib.sha256(self.data).hexdigest()
            or len(self.data) != self.received_bytes
        ):
            raise InvalidInputError("fetch bytes do not match outcome digest")
        if self.status == FetchStatus.FETCHED and self.data is None:
            raise InvalidInputError("a fetched result requires bytes")


async def fetch_approved(permit: FetchPermit, *, client: httpx.AsyncClient) -> FetchResult:
    """Stream one exact HTTPS URL through an injected, centrally controlled client.

    Caller supplies a client constructed with trusted proxy/egress policy;
    this function never forwards arbitrary request headers or credentials.
    A service-specific adapter may enrich requests but must retain the permit.
    """
    if not isinstance(permit, FetchPermit):
        raise InvalidInputError("an explicit fetch permit is required")

    def result(status: FetchStatus, code: int | None = None,
               data: bytes | None = None, count: int = 0) -> FetchResult:
        return FetchResult(status, permit.action_id, permit.exact_url, code, data,
                           hashlib.sha256(data).hexdigest() if data is not None else None, count)

    try:
        async with asyncio.timeout(permit.deadline_seconds):
            # Standard httpx.AsyncClient interface, including MockTransport.
            async with client.stream("GET", permit.exact_url, follow_redirects=False) as response:
                code = response.status_code
                if 300 <= code < 400:
                    return result(FetchStatus.REDIRECT_DENIED, code)
                if code == 429:
                    return result(FetchStatus.RATE_LIMITED, code)
                if code < 200 or code >= 300:
                    return result(FetchStatus.UNAVAILABLE, code)
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        length = int(content_length)
                    except ValueError:
                        return result(FetchStatus.UNAVAILABLE, code)
                    if length < 0 or length > permit.max_bytes:
                        return result(FetchStatus.RESPONSE_TOO_LARGE, code)
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(buffer) + len(chunk) > permit.max_bytes:
                        return result(FetchStatus.RESPONSE_TOO_LARGE, code,
                                      count=len(buffer) + len(chunk))
                    buffer.extend(chunk)
                return result(FetchStatus.FETCHED, code, bytes(buffer), len(buffer))
    except TimeoutError:
        return result(FetchStatus.TIMED_OUT)
    except Exception as error:
        # Do not expose private network errors or potentially secret-bearing
        # redirect details through recorded public result fields.
        import httpx

        if isinstance(error, (httpx.HTTPError, OSError)):
            return result(FetchStatus.UNAVAILABLE)
        raise
