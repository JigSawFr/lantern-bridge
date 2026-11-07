# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""An Ownfoil as a source: its GraphQL API gives the files, the sheets and the images.

Ownfoil knows more than a plain Tinfoil index: what each file is (id, version, kind), the name and the
description of the game, its artwork. The files are read like any shop's (the download address comes with the
listing, and is relayed with Range), the sheets and images come from the same API, and ``/api/ws`` says when
something changed, so that the library does not wait for the next scan.

Ownfoil is not trusted any more than a Tinfoil shop: the same origin rule, the same caps on what is read.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from lantern.server.file_index import Entry
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from lantern_bridge.identify import identify
from lantern_bridge.sources.tinfoil import (
    MAX_DEPTH,
    MAX_FILES,
    Listed,
    Shop,
    UpstreamError,
    depth,
    index_path,
    origin,
)

log = logging.getLogger("lantern_bridge.ownfoil")

#: The most bytes read from one answer of the API (a page of apps or of titles).
MAX_ANSWER = 8 << 20
#: The most bytes of one image; Ownfoil's own renditions are far smaller.
MAX_IMAGE = 8 << 20
#: The most screenshots kept for a title.
MAX_SCREENSHOTS = 4
APPS_PAGE = 500
TITLES_PAGE = 200
#: The most bytes of one message of the websocket.
MAX_MESSAGE = 1 << 20

_APPS = (
    "query($page:Int!,$size:Int!){apps(owned:true,page:$page,pageSize:$size){total items{"
    "appId appVersion downloadUrl downloadSize downloadExtension title{name}}}}"
)
_TITLES = (
    "query($page:Int!,$size:Int!){titles(owned:true,page:$page,pageSize:$size){total items{"
    "titleId name intro description developer publisher releaseDate category languages "
    "icon(size:CLIENT){url local width height} banner(size:CLIENT){url local width height} "
    "screenshots(size:CLIENT){url local width height}}}}"
)

_TITLE_ID = re.compile(r"[0-9A-F]{16}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_LANGUAGE = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")
_MARKS = re.compile(r"[\[\]#/\\\x00-\x1f\x7f]")
#: The first bytes of the images a Lantern server may show, and their extension.
_IMAGES = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"), (b"GIF87a", "gif"), (b"GIF89a", "gif"))
#: What Ownfoil's categories (the eShop's) are called in Lantern's list of genres; the others stay tags.
_GENRES = {
    "action": "action",
    "adventure": "adventure",
    "role-playing": "rpg",
    "role playing": "rpg",
    "rpg": "rpg",
    "strategy": "strategy",
    "simulation": "simulation",
    "sports": "sports",
    "racing": "racing",
    "puzzle": "puzzle",
    "platformer": "platformer",
    "platform": "platformer",
    "shooter": "shooter",
    "fighting": "fighting",
    "music": "music",
    "party": "party",
    "education": "education",
    "educational": "education",
}
_FORMATS = frozenset({"nsp", "nsz", "xci", "xcz"})
_FINISHED = frozenset({"completed", "failed"})


@dataclass(frozen=True, slots=True)
class Picture:
    """An image Ownfoil keeps: its address (inside the shop) and its size, as Ownfoil measured it."""

    address: str
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class Sheet:
    """What Ownfoil says of a game."""

    title_id: str
    name: str | None = None
    summary: str | None = None
    description: str | None = None
    developer: str | None = None
    publisher: str | None = None
    release: str | None = None
    genres: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    icon: Picture | None = None
    banner: Picture | None = None
    screenshots: tuple[Picture, ...] = ()

    def pictures(self) -> Iterator[Picture]:
        for picture in (self.icon, self.banner, *self.screenshots):
            if picture is not None:
                yield picture


def image_kind(data: bytes) -> str | None:
    """The extension of an image the library may serve (judged by its first bytes, never by what the shop
    says), or None."""
    for magic, extension in _IMAGES:
        if data.startswith(magic):
            return extension
    return "webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None


def _text(value: object, limit: int = 4000) -> str | None:
    return value.strip()[:limit] or None if isinstance(value, str) else None


def _words(value: object, pattern: re.Pattern[str] | None = None) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    found = [v.strip()[:80] for v in value[:64] if isinstance(v, str) and v.strip()]
    return tuple(v for v in found if pattern is None or pattern.fullmatch(v))


def file_name(title: object, app_id: str, version: int, extension: str) -> str:
    """The name the library identifies a file by: ``Title [APPID][vVERSION].ext``, built from what Ownfoil says."""
    name = _MARKS.sub("", title).strip()[:120] if isinstance(title, str) else ""
    return f"{name or app_id} [{app_id}][v{version}].{extension}"


class Ownfoil(Shop):
    """One Ownfoil: a `Shop` whose list of files, sheets and images come from its GraphQL API."""

    def __init__(
        self, url: str, *, user: str | None = None, password: str | None = None, client: httpx.Client | None = None
    ) -> None:
        super().__init__(url, user=user, password=password, client=client)
        self._basic = (
            "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
            if user is not None and password is not None
            else None
        )

    # The API.

    def query(self, text: str, **variables: int) -> dict[str, Any]:
        """The `data` of one GraphQL query. Any error, or an answer that is not what a GraphQL server gives, is
        an UpstreamError that says nothing of what the shop wrote."""
        body = json.dumps({"query": text, "variables": variables}).encode()
        reply = self.get(urljoin(self.url, "/api/graphql"), limit=MAX_ANSWER, body=body)
        try:
            document = json.loads(reply)
        except (ValueError, RecursionError) as error:
            raise UpstreamError("the GraphQL answer is not valid JSON") from error
        if not isinstance(document, dict) or depth(document) > MAX_DEPTH:
            raise UpstreamError("the GraphQL answer is not an object, or nests too deep")
        data = document.get("data")
        if document.get("errors") or not isinstance(data, dict):
            raise UpstreamError("the GraphQL API answered an error")
        return data

    def _pages(self, text: str, field: str, size: int) -> Iterator[dict[str, Any]]:
        """Every item of a paged query, in order, at most `MAX_FILES` of them."""
        seen, page = 0, 1
        while seen < MAX_FILES:
            connection = self.query(text, page=page, size=size).get(field)
            items = connection.get("items") if isinstance(connection, dict) else None
            if not isinstance(items, list) or not items:
                return
            for item in items:
                if isinstance(item, dict):
                    yield item
            seen += len(items)
            total = connection.get("total") if isinstance(connection, dict) else None
            if len(items) < size or (isinstance(total, int) and seen >= total):
                return
            page += 1

    # Files.

    def listing(self) -> Iterator[Listed]:
        """The files of the library: one per app Ownfoil holds, named from what it knows of it."""
        here = origin(self.url)
        for item in self._pages(_APPS, "apps", APPS_PAGE):
            link, size = item.get("downloadUrl"), item.get("downloadSize")
            app_id, version, extension = item.get("appId"), item.get("appVersion"), item.get("downloadExtension")
            if extension not in _FORMATS or not isinstance(link, str) or not isinstance(app_id, str):
                continue
            if isinstance(size, bool):
                continue
            if not isinstance(size, int) or size < 0 or not isinstance(version, int) or isinstance(version, bool):
                continue
            absolute = urljoin(self.url, link)
            if origin(absolute) != here or urlsplit(absolute).username:
                continue
            title = item.get("title")
            name = file_name(title.get("name") if isinstance(title, dict) else None, app_id, version, extension)
            yield Listed(absolute, name, size)

    def scan(self) -> Iterator[Entry]:
        """The files as index entries (a file whose name does not identify it is left out)."""
        for listed in self.listing():
            identity = identify(listed.name)
            if identity is not None:
                yield Entry(index_path(listed), listed.size, 0, identity.priority)

    # Sheets and images.

    def _picture(self, raw: object) -> Picture | None:
        """An image Ownfoil serves itself (`local`); one it only points to elsewhere is not followed."""
        if not isinstance(raw, dict) or raw.get("local") is not True or not isinstance(raw.get("url"), str):
            return None
        absolute = urljoin(self.url, raw["url"])
        if origin(absolute) != origin(self.url) or urlsplit(absolute).username:
            return None
        sizes = [
            v if isinstance(v := raw.get(k), int) and not isinstance(v, bool) and 0 < v <= 20000 else None
            for k in ("width", "height")
        ]
        return Picture(absolute, sizes[0], sizes[1]) if sizes[0] and sizes[1] else Picture(absolute)

    def sheets(self) -> dict[str, Sheet]:
        """The sheet of every game of the library, by title id."""
        found: dict[str, Sheet] = {}
        for item in self._pages(_TITLES, "titles", TITLES_PAGE):
            title_id = item.get("titleId")
            if not isinstance(title_id, str) or not _TITLE_ID.fullmatch(title_id):
                continue
            release = item.get("releaseDate")
            shots = item.get("screenshots")
            categories = _words(item.get("category"))
            genres = tuple(dict.fromkeys(g for c in categories if (g := _GENRES.get(c.lower()))))
            found[title_id] = Sheet(
                title_id=title_id,
                name=_text(item.get("name"), 200),
                summary=_text(item.get("intro"), 500),
                description=_text(item.get("description")),
                developer=_text(item.get("developer"), 200),
                publisher=_text(item.get("publisher"), 200),
                release=release if isinstance(release, str) and _DATE.fullmatch(release) else None,
                genres=genres,
                tags=tuple(c for c in categories if c.lower() not in _GENRES),
                languages=_words(item.get("languages"), _LANGUAGE),
                icon=self._picture(item.get("icon")),
                banner=self._picture(item.get("banner")),
                screenshots=tuple(
                    p
                    for p in (self._picture(s) for s in (shots[:MAX_SCREENSHOTS] if isinstance(shots, list) else []))
                    if p
                ),
            )
        return found

    def image(self, picture: Picture) -> tuple[bytes, str]:
        """The bytes of an image and its extension; UpstreamError when it is not an image the library serves."""
        data = self.get(picture.address, limit=MAX_IMAGE, accept="image/*")
        extension = image_kind(data)
        if extension is None:
            raise UpstreamError("the shop's image is not a png, jpg, webp or gif")
        return data, extension

    # Changes.

    def watch(self, changed: Callable[[], None], stop: threading.Event) -> None:
        """Tell `changed` each time a task of Ownfoil finishes (a scan, an identification…), until `stop` is set.

        Reads ``/api/ws?topics=tasks`` of the shop, which only an administrator may. When the shop does not
        allow it (or has no such endpoint) the bridge keeps to its regular reads, and tries again in a while.
        """
        parts = urlsplit(self.url)
        address = (
            f"{'wss' if parts.scheme == 'https' else 'ws'}://{parts.netloc.rsplit('@', 1)[-1]}/api/ws?topics=tasks"
        )
        headers = {"Authorization": self._basic} if self._basic else {}
        wait, said = 30.0, False
        while not stop.is_set():
            try:
                with connect(address, additional_headers=headers, max_size=MAX_MESSAGE, open_timeout=10) as socket:
                    wait = 30.0
                    while not stop.is_set():
                        try:
                            message = socket.recv(timeout=5)
                        except TimeoutError:
                            continue
                        if _finished(message):
                            changed()
            except (WebSocketException, OSError) as error:
                if not said:
                    log.info(
                        "%s: no live changes (%s); the shop is read every few minutes", self.url, type(error).__name__
                    )
                    said = True
                stop.wait(wait)
                wait = min(wait * 2, 600.0)


def _finished(message: str | bytes) -> bool:
    """Whether a message of the websocket says a task ended."""
    try:
        event = json.loads(message)
    except (ValueError, RecursionError):
        return False
    data = event.get("data") if isinstance(event, dict) else None
    return (
        isinstance(event, dict)
        and event.get("topic") == "tasks"
        and event.get("type") in ("add", "update")
        and isinstance(data, dict)
        and data.get("status") in _FINISHED
    )
