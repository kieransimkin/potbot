"""Exact DNS allowlists and numeric address selection for HTTPS egress."""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from potbot.domain.errors import InvalidInputError, ScopeDeniedError

# Azure platform virtual IP uses a globally numbered address.
_PLATFORM_IPS = frozenset({ipaddress.ip_address("168.63.129.16")})
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")


def checked_host(host: object) -> str:
    """Allow only canonical ASCII DNS names (never IPs, Unicode/IDNA aliases)."""
    if (
        not isinstance(host, str)
        or host != host.lower()
        or len(host) > 253
        or (
            not _HOST.fullmatch(host)
            or "." not in host
            or host.endswith(".")
            or any(
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                or label.startswith("xn--")
                for label in host.split(".")
            )
        )
    ):
        raise InvalidInputError("CONNECT host must be a canonical ASCII DNS name")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ScopeDeniedError("CONNECT to IP addresses is forbidden")


@dataclass(frozen=True, slots=True)
class ConnectPolicy:
    hosts: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.hosts, tuple) or not 1 <= len(self.hosts) <= 32:
            raise InvalidInputError("CONNECT requires a small explicit exact-host allowlist")
        for h in self.hosts:
            checked_host(h)
        if len(set(self.hosts)) != len(self.hosts):
            raise InvalidInputError("duplicate CONNECT policy host")

    def resolve(
        self, authority: str, *, resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo
    ) -> str:
        if not isinstance(authority, str) or authority.count(":") != 1:
            raise ScopeDeniedError("CONNECT must specify one approved hostname and TLS port")
        host, port = authority.split(":", 1)
        host = checked_host(host)
        if host not in self.hosts or port != "443":
            raise ScopeDeniedError("CONNECT destination is not on the operator policy")
        try:
            answers = resolver(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        except OSError as error:
            raise ScopeDeniedError("approved destination is not currently resolvable") from error
        if not answers or len(answers) > 64:
            raise ScopeDeniedError("CONNECT DNS answer is empty or excessive")
        addresses = []
        for answer in answers:
            address = answer[4][0]
            try:
                ip = ipaddress.ip_address(address)
            except ValueError as error:
                raise ScopeDeniedError("unexpected CONNECT DNS address") from error
            if ip.version == 6 and ip.ipv4_mapped is not None:
                ip = ip.ipv4_mapped
            # Entire answer must be public: mixed public/private is suspect.
            if not ip.is_global or ip.is_multicast or ip.is_unspecified or ip in _PLATFORM_IPS:
                raise ScopeDeniedError("CONNECT DNS includes a private or special address")
            addresses.append(str(ip))
        # connect *directly* to the checked literal IP; never resolve hostname
        # a second time inside the transport, avoiding classic DNS rebinding.
        return sorted(set(addresses))[0]
