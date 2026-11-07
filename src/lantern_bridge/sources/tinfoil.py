# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""A Tinfoil shop as a source: read its JSON index, and follow nothing outside its origin.

The shop is not trusted. It may list any URL, send a huge or deeply nested document, redirect elsewhere or
answer forever; so every address must keep the origin of the configured one, every read is capped in size and
time, the document's depth and the number of entries are limited, and a name that would leave a folder is dropped.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from lantern.server.file_index import Entry

from lantern_bridge.identify import identify

log = logging.getLogger("lantern_bridge.tinfoil")

#: The most bytes read from one index; a shop of 100 000 files stays well under it.
MAX_INDEX = 16 << 20
MAX_DEPTH = 16
MAX_FILES = 200_000
MAX_DIRECTORIES = 64
MAX_REDIRECTS = 3
MAX_NAME = 255
TIMEOUT = httpx.Timeout(10.0, read=30.0)
#: Sent on every call: AeroFoil (and other shops) serve their JSON index only to a client that names itself
#: a Tinfoil one in its User-Agent, and answer a web page to anything else.
USER_AGENT = "Tinfoil-compatible lantern-bridge"


class UpstreamError(Exception):
    """The shop cannot be read; the message says why (never a secret)."""


@dataclass(frozen=True, slots=True)
class Listed:
    """A file the shop lists: its address, the name it goes by and its size."""

    url: str
    name: str
    size: int


def origin(url: str) -> tuple[str, str, int]:
    """(scheme, host, port) of an address, the default port filled in."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), parts.port or (443 if scheme == "https" else 80)


def depth(value: Any, limit: int = MAX_DEPTH) -> int:
    """How deeply a parsed document nests, counted without recursion; stops once past `limit`."""
    top, stack = 0, [(value, 1)]
    while stack:
        item, level = stack.pop()
        if isinstance(item, (dict, list)):
            top = max(top, level)
            if top > limit:
                return top
            stack.extend((child, level + 1) for child in (item.values() if isinstance(item, dict) else item))
    return top


def parse_index(data: bytes) -> dict[str, Any]:
    """The index document: a JSON object, not too deep. Raises UpstreamError otherwise."""
    try:
        document = json.loads(data)
    except (ValueError, RecursionError) as error:
        raise UpstreamError("the index is not valid JSON") from error
    if not isinstance(document, dict):
        raise UpstreamError("the index is not a JSON object")
    if depth(document) > MAX_DEPTH:
        raise UpstreamError(f"the index nests deeper than {MAX_DEPTH} levels")
    return document


def safe_name(name: str) -> str | None:
    """A file name that stays a name: no separator, no control character, no dot name, not too long."""
    if not name or len(name) > MAX_NAME or name in {".", ".."}:
        return None
    if any(c in name for c in "/\\") or any(ord(c) < 32 or ord(c) == 127 for c in name):
        return None
    return name


def _fields(raw: object) -> tuple[object, object]:
    """The url and the size of an index entry (a bare URL has no size)."""
    if isinstance(raw, dict):
        return raw.get("url"), raw.get("size")
    return raw, None


def entries(document: dict[str, Any], base: str) -> Iterator[Listed]:
    """The files of an index. `base` is its address: a relative entry is read against it.

    An entry is a URL string or an object with `url` and `size`; the part after `#` is the name the shop gives.
    An entry with no usable size, no name Lantern can identify or another origin is skipped.
    """
    files = document.get("files", [])
    if not isinstance(files, list):
        return
    here = origin(base)
    for raw in files[:MAX_FILES]:
        url, size = _fields(raw)
        if not isinstance(url, str) or isinstance(size, bool) or not isinstance(size, int) or size < 0:
            continue
        absolute = urljoin(base, url)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https") or origin(absolute) != here or parts.username or parts.password:
            continue
        name = safe_name(unquote(parts.fragment) if parts.fragment else unquote(parts.path.rsplit("/", 1)[-1]))
        if name is None:
            continue
        yield Listed(absolute.split("#", 1)[0], name, size)


def directories(document: dict[str, Any], base: str) -> list[str]:
    """The addresses of the further indexes the document names (same origin only)."""
    raw = document.get("directories", [])
    if not isinstance(raw, list):
        return []
    here, out = origin(base), []
    for item in raw[:MAX_DIRECTORIES]:
        url = item if isinstance(item, str) else item.get("url") if isinstance(item, dict) else None
        if isinstance(url, str):
            absolute = urljoin(base, url).split("#", 1)[0]
            if urlsplit(absolute).scheme in ("http", "https") and origin(absolute) == here:
                out.append(absolute)
    return out


def index_path(listed: Listed) -> str:
    """The key of a file in the index: its address inside the shop as the shop wrote it (path and query, no
    leading slash), then `#` and the name. `Shop.address` gives the address back."""
    parts = urlsplit(listed.url)
    return parts.path.lstrip("/") + (f"?{parts.query}" if parts.query else "") + "#" + listed.name


class Shop:
    """One Tinfoil shop. `client` is injectable for tests; redirects are followed here, not by httpx."""

    def __init__(
        self,
        url: str,
        *,
        user: str | None = None,
        password: str | None = None,
        client: httpx.Client | None = None,
        hauth: str | None = None,
    ) -> None:
        self.url = url
        self.hauth = hauth
        self._auth = httpx.BasicAuth(user, password) if user is not None and password is not None else None
        self._client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
        self._owns = client is None

    def headers(self, **extra: str) -> dict[str, str]:
        """What every call sends: the Tinfoil User-Agent, and the host-verification value when the shop has one."""
        out = {"User-Agent": USER_AGENT, **extra}
        if self.hauth:
            out["Hauth"] = self.hauth
        return out

    def address(self, path: str) -> str:
        """The URL of the file an index key stands for: always inside the shop's origin."""
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}/{path.rsplit('#', 1)[0]}"

    def close(self) -> None:
        if self._owns:
            self._client.close()

    def get(
        self, url: str, *, limit: int = MAX_INDEX, body: bytes | None = None, accept: str = "application/json"
    ) -> bytes:
        """The body at `url` (POSTing `body` as JSON when given), read at most `limit` bytes; a redirect is
        followed only inside the origin."""
        here = origin(self.url)
        for _ in range(MAX_REDIRECTS + 1):
            if origin(url) != here:
                raise UpstreamError("the shop sent the bridge to another origin")
            try:
                headers = self.headers(Accept=accept)
                if body is not None:
                    headers["Content-Type"] = "application/json"
                method = "GET" if body is None else "POST"
                with self._client.stream(method, url, auth=self._auth, headers=headers, content=body) as reply:
                    if reply.status_code in (301, 302, 303, 307, 308):
                        url = urljoin(url, reply.headers.get("location", ""))
                        continue
                    if reply.status_code != 200:
                        raise UpstreamError(f"the shop answered {reply.status_code}")
                    received = bytearray()
                    for chunk in reply.iter_bytes():
                        received += chunk
                        if len(received) > limit:
                            raise UpstreamError(f"the answer is larger than {limit >> 20} MiB")
                    return bytes(received)
            except httpx.HTTPError as error:
                raise UpstreamError(f"the shop cannot be reached ({type(error).__name__})") from error
        raise UpstreamError("too many redirects")

    def stream(self, path: str, first: int = 0, length: int | None = None) -> Iterator[bytes]:
        """The bytes of a file of the shop (`length` of them from `first`, or all), as they arrive.

        Only the shop's origin is read; a redirect is followed inside it. A short or failed read raises
        UpstreamError, so that nothing is taken for the whole file.
        """
        url = self.address(path)
        headers = self.headers()
        if first or length is not None:
            headers["Range"] = f"bytes={first}-" + ("" if length is None else str(first + length - 1))
        for _ in range(MAX_REDIRECTS + 1):
            if origin(url) != origin(self.url):
                raise UpstreamError("the shop sent the bridge to another origin")
            try:
                with self._client.stream("GET", url, auth=self._auth, headers=headers) as reply:
                    if reply.status_code in (301, 302, 303, 307, 308):
                        url = urljoin(url, reply.headers.get("location", ""))
                        continue
                    wanted = 206 if headers else 200
                    if reply.status_code != wanted and not (wanted == 206 and reply.status_code == 200 and first == 0):
                        raise UpstreamError(f"the shop answered {reply.status_code}")
                    left = length
                    for piece in reply.iter_bytes(1 << 20):
                        chunk = piece if left is None else piece[:left]
                        if left is not None:
                            left -= len(chunk)
                        if chunk:
                            yield chunk
                        if left == 0:
                            return
                    if left:
                        raise UpstreamError("the shop closed the file early")
                    return
            except httpx.HTTPError as error:
                raise UpstreamError(f"the shop cannot be read ({type(error).__name__})") from error
        raise UpstreamError("too many redirects")

    def listing(self) -> Iterator[Listed]:
        """Every file of the shop: its index, then the directories it names, breadth first and bounded."""
        queue, seen, files = [self.url], {self.url}, 0
        while queue and len(seen) <= MAX_DIRECTORIES + 1:
            address = queue.pop(0)
            try:
                document = parse_index(self.get(address))
            except UpstreamError as error:
                if address == self.url:
                    raise
                log.warning("directory skipped: %s", error)
                continue
            for listed in entries(document, address):
                files += 1
                if files > MAX_FILES:
                    log.warning("the shop lists more than %d files: the rest is ignored", MAX_FILES)
                    return
                yield listed
            for nxt in directories(document, address):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)

    def scan(self) -> Iterator[Entry]:
        """The packages the names identify, as index entries (size known, no date: the shop gives none)."""
        for listed in self.listing():
            if identify(listed.name) is None:
                continue
            identity = identify(listed.name)
            if identity is not None:
                yield Entry(index_path(listed), listed.size, 0, identity.priority)
