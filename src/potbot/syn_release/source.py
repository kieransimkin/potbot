"""Canonical GitHub acquisition with bounded reads and credential-free redirects."""

from __future__ import annotations

import io
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, BinaryIO

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, SourceUnavailableError
from potbot.syn_release.contracts import DOCUMENT_LIMIT, REPOSITORY, Policy, document

_API = f"https://api.github.com/repos/{REPOSITORY}"
_ASSET_HOSTS = {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


@dataclass(frozen=True)
class RemoteAsset:
    identifier: int
    name: str
    size: int


@dataclass(frozen=True)
class Publication:
    identifier: int
    assets: tuple[RemoteAsset, ...]


class GitHubSource:
    """No arbitrary repository, URL, branch, latest lookup or caller redirect list."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token
        self._opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
        )

    def _read(self, url: str, output: BinaryIO, *, limit: int, asset: bool = False) -> int:
        origin = url
        for attempt in range(2):
            output.seek(0)
            output.truncate()
            current = origin
            deadline = time.monotonic() + 60
            try:
                for _redirect in range(4):
                    parsed = urllib.parse.urlsplit(current)
                    api_request = current.startswith(_API + "/")
                    if (
                        parsed.scheme != "https"
                        or parsed.username
                        or parsed.password
                        or parsed.port not in {None, 443}
                        or parsed.fragment
                        or (not api_request and (not asset or parsed.hostname not in _ASSET_HOSTS))
                    ):
                        raise ScopeDeniedError("Untrusted Syn download redirect")
                    headers = {
                        "Accept": "application/octet-stream"
                        if asset
                        else "application/vnd.github+json",
                        "User-Agent": "potbot-syn-release/1",
                        "X-GitHub-Api-Version": "2026-03-10",
                        "Accept-Encoding": "identity",
                    }
                    if api_request and self._token:
                        headers["Authorization"] = "Bearer " + self._token
                    request = urllib.request.Request(current, headers=headers)
                    try:
                        response = self._opener.open(request, timeout=10)
                    except urllib.error.HTTPError as error:
                        if error.code in {301, 302, 303, 307, 308}:
                            target = error.headers.get("Location")
                            error.close()
                            if not asset or not target:
                                raise ScopeDeniedError("Unexpected Syn metadata redirect") from None
                            current = urllib.parse.urljoin(current, target)
                            continue
                        if error.code >= 500:
                            error.close()
                            raise OSError("temporary upstream failure") from None
                        error.close()
                        raise SourceUnavailableError(
                            "Selected private Syn release is unavailable; check access and exact pin"
                        ) from None
                    with response:
                        if response.status != 200:
                            raise SourceUnavailableError(
                                "Syn download did not return complete bytes"
                            )
                        declared = response.headers.get("Content-Length")
                        if declared is not None and (
                            not declared.isdecimal() or int(declared) > limit
                        ):
                            raise InvalidInputError("Syn download exceeds its declared bound")
                        total = 0
                        while True:
                            if time.monotonic() > deadline:
                                raise TimeoutError("Syn read deadline")
                            chunk = response.read(min(65_536, limit - total + 1))
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > limit:
                                raise InvalidInputError("Syn download exceeds its byte bound")
                            output.write(chunk)
                        if declared is not None and total != int(declared):
                            raise SourceUnavailableError(
                                "Syn download ended before its declared size"
                            )
                        return total
                raise ScopeDeniedError("Too many Syn download redirects")
            except (OSError, urllib.error.URLError):
                if attempt:
                    raise SourceUnavailableError(
                        "Syn download failed within its bounded retry budget"
                    ) from None
        raise AssertionError("unreachable")

    def _json(self, suffix: str) -> dict[str, Any]:
        output = io.BytesIO()
        self._read(_API + suffix, output, limit=DOCUMENT_LIMIT)
        return document(output.getvalue())

    def publication(self, policy: Policy) -> Publication:
        value = self._json("/releases/tags/" + policy.tag)
        if (
            value.get("draft") is not False
            or value.get("prerelease") is not False
            or value.get("immutable") is not True
            or value.get("tag_name") != policy.tag
            or value.get("html_url") != f"https://github.com/{REPOSITORY}/releases/tag/{policy.tag}"
            or type(value.get("id")) is not int
            or value["id"] <= 0
            or type(value.get("assets")) is not list
            or len(value["assets"]) != 7
        ):
            raise ScopeDeniedError(
                "Syn release is not the selected immutable seven-asset publication"
            )
        reference = self._json("/git/ref/tags/" + policy.tag)
        target = reference.get("object")
        if (
            not isinstance(target, dict)
            or target.get("type") != "commit"
            or target.get("sha") != policy.value["source_commit"]
        ):
            raise ScopeDeniedError("Syn release tag differs from the selected source commit")
        assets = []
        for entry in value["assets"]:
            if (
                not isinstance(entry, dict)
                or type(entry.get("id")) is not int
                or entry["id"] <= 0
                or not isinstance(entry.get("name"), str)
                or entry.get("state") != "uploaded"
                or type(entry.get("size")) is not int
                or entry["size"] <= 0
            ):
                raise InvalidInputError("Invalid Syn publication asset metadata")
            assets.append(RemoteAsset(entry["id"], entry["name"], entry["size"]))
        if len({a.name for a in assets}) != 7 or len({a.identifier for a in assets}) != 7:
            raise InvalidInputError("Duplicate Syn publication assets")
        return Publication(value["id"], tuple(assets))

    def download(self, asset: RemoteAsset, output: BinaryIO) -> int:
        return self._read(
            f"{_API}/releases/assets/{asset.identifier}", output, limit=asset.size, asset=True
        )


def publication_record(publication: Publication) -> bytes:
    return json.dumps(
        {
            "release_id": publication.identifier,
            "assets": [
                {"id": a.identifier, "name": a.name, "size": a.size} for a in publication.assets
            ],
        },
        sort_keys=True,
    ).encode()
