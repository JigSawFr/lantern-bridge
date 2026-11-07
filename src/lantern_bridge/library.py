# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The library the server shows: the hashed files of the index, as Lantern apps.

The SDK's ``MemoryLibrary`` holds and serves the catalogue (and its journal);
this module only keeps it equal to the index. A file appears once it is hashed,
disappears when it is gone or changed, and is never hashed again here.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

from lantern._generated.models import App, Image, Title
from lantern.server.files import FileRef
from lantern.server.library import MemoryLibrary

from lantern_bridge.identify import Identity, identify
from lantern_bridge.index import Index, Row
from lantern_bridge.sources.ownfoil import Ownfoil, Picture, Sheet
from lantern_bridge.sources.tinfoil import Shop, UpstreamError

log = logging.getLogger("lantern_bridge.library")


class Projection:
    """Keeps a ``MemoryLibrary`` equal to the hashed rows of an ``Index``.

    The same bytes (the same SHA-256) under several paths or sources are one app, served by the best source:
    a folder before a shop, then the order of the configuration, then the path. A file of a shop is relayed.
    """

    def __init__(
        self,
        library: MemoryLibrary,
        index: Index,
        roots: dict[str, Path],
        shops: dict[str, Shop] | None = None,
        order: Sequence[str] = (),
    ) -> None:
        self.library = library
        self.index = index
        self.roots = roots
        self.shops = shops if shops is not None else {}
        self.order = {source: n for n, source in enumerate(order)}
        self._shown: dict[str, tuple[Row, Identity]] = {}  # app id -> the row that serves it
        self._refused: dict[str, Row] = {}  # what the library refused, so that it is said once
        self._titles: dict[str, Title] = {}
        self.sheets: dict[str, dict[str, Sheet]] = {}  # source -> title id -> what the source says of the game
        self._pictures: dict[tuple[str, str], Image] = {}  # (source, address) -> the image as the library serves it
        self._picture_retry: dict[tuple[str, str], float] = {}

    def served(self) -> tuple[int, int]:
        """How many apps and how many titles the library offers now."""
        return len(self._shown), len({identity.title_id for _, identity in self._shown.values()})

    def serving(self, row: Row) -> bool:
        """Is this very file the one that serves its app (not a copy of it in another source)?"""
        if row.sha256 is None:
            return False
        shown = self._shown.get("a_" + row.sha256[:12])
        return shown is not None and shown[0] == row

    def title(self, title_id: str) -> Title | None:
        """The sheet the library gives a title now."""
        return self._titles.get(title_id)

    def _rank(self, row: Row) -> tuple[int, int, str]:
        return (row.source not in self.roots, self.order.get(row.source, len(self.order)), row.path)

    def _file(self, row: Row, sha256: str) -> FileRef | None:
        """How the bytes of a row are served: from its folder, or relayed from its shop."""
        if (root := self.roots.get(row.source)) is not None:
            return FileRef(str(root / row.path), row.size, sha256)
        if (shop := self.shops.get(row.source)) is not None:
            path = row.path

            def read(first: int, length: int) -> Iterator[bytes]:
                return shop.stream(path, first, length)

            return FileRef("", row.size, sha256, read=read)
        return None

    def refresh(self) -> tuple[int, int]:
        """Add what became listable, remove what is gone or served better elsewhere; returns (added, removed)."""
        groups: dict[str, list[tuple[Row, Identity, FileRef]]] = {}
        for row in self.index.hashed():
            identity = identify(row.path)
            ref = self._file(row, row.sha256) if row.sha256 is not None else None
            if row.sha256 is not None and identity is not None and ref is not None:
                groups.setdefault(row.sha256, []).append((row, identity, ref))
        wanted: dict[str, tuple[Row, Identity, FileRef]] = {}
        for sha256, candidates in groups.items():
            wanted["a_" + sha256[:12]] = min(candidates, key=lambda c: self._rank(c[0]))
        removed = 0
        for app_id, (row, _) in list(self._shown.items()):
            if app_id not in wanted or wanted[app_id][0] != row:
                self.library.remove_app(app_id)
                del self._shown[app_id]
                removed += 1
        added = 0
        for app_id, (row, identity, ref) in wanted.items():
            if app_id in self._shown or self._refused.get(app_id) == row:
                continue
            app = App(
                id=app_id,
                platform="switch",
                title_id=identity.title_id,
                app_id=identity.app_id,
                type=identity.type,
                version_code=identity.version_code,
                name=identity.name,
                size=row.size,
                format=identity.format,
                sha256=ref.sha256,
                file="",
            )
            try:
                self.library.add_app(app, ref)
            except ValueError as error:
                log.warning("not listed: %s/%s (%s)", row.source, row.path, error)
                self._refused[app_id] = row
                continue
            self._refused.pop(app_id, None)
            self._shown[app_id] = (row, identity)
            added += 1
        self._refresh_titles()
        return added, removed

    def _sheet(self, title_id: str) -> Sheet | None:
        """The sheet a source gives for a game: the first source of the configuration that has one."""
        found = [
            (self.order.get(source, len(self.order)), sheet)
            for source, sheets in self.sheets.items()
            if (sheet := sheets.get(title_id)) is not None
        ]
        return min(found, key=lambda f: f[0])[1] if found else None

    def _image(self, source: str, picture: Picture | None) -> Image | None:
        if picture is None:
            return None
        return self._pictures.get((source, picture.address))

    def _title(self, title_id: str, name: str) -> Title:
        """The sheet of a title: what its source says, else the name the files give."""
        found = [
            (self.order.get(source, len(self.order)), source, sheets[title_id])
            for source, sheets in self.sheets.items()
            if title_id in sheets
        ]
        if not found:
            return Title(platform="switch", title_id=title_id, name=name)
        _, source, sheet = min(found, key=lambda f: f[0])
        shots = [image for p in sheet.screenshots if (image := self._image(source, p)) is not None]
        return Title(
            platform="switch",
            title_id=title_id,
            name=sheet.name or name,
            summary=sheet.summary,
            description=sheet.description,
            developer=sheet.developer,
            publisher=sheet.publisher,
            release=sheet.release,
            genres=list(sheet.genres) or None,
            tags=list(sheet.tags) or None,
            languages=list(sheet.languages) or None,
            icon=self._image(source, sheet.icon),
            banner=self._image(source, sheet.banner),
            screenshots=shots or None,
        )

    def fetch_pictures(self, limit: int = 50) -> int:
        """Read the images the sheets name and the library does not hold yet (at most `limit` at a time);
        returns how many it took. An image that cannot be read is tried again in a quarter of an hour."""
        taken, now = 0, time.monotonic()
        for source, sheets in self.sheets.items():
            shop = self.shops.get(source)
            if not isinstance(shop, Ownfoil):
                continue
            for sheet in sheets.values():
                for picture in sheet.pictures():
                    key = (source, picture.address)
                    if key in self._pictures or self._picture_retry.get(key, 0) > now:
                        continue
                    if taken >= limit:
                        return taken
                    try:
                        data, extension = shop.image(picture)
                        ref = FileRef.of_bytes(
                            data,
                            {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}[
                                extension
                            ],
                        )
                        name = self.library.add_media(ref, extension)
                    except (UpstreamError, ValueError) as error:
                        log.warning("source %s: an image cannot be taken (%s)", source, error)
                        self._picture_retry[key] = now + 900.0
                        continue
                    self._pictures[key] = Image(
                        url=f"{self.library.base}/v1/media/{name}",
                        width=picture.width,
                        height=picture.height,
                        sha256=ref.sha256,
                    )
                    taken += 1
        return taken

    def _refresh_titles(self) -> None:
        """One title sheet for each title id an app belongs to (SPEC.md § 7.1): the base game names it."""
        wanted: dict[str, str] = {}
        for _, identity in sorted(self._shown.values(), key=lambda v: v[1].priority, reverse=True):
            wanted[identity.title_id] = identity.name  # the base comes last, and wins
        for title_id in set(self._titles) - set(wanted):
            self.library.remove_title("switch", title_id)
            del self._titles[title_id]
        for title_id, name in wanted.items():
            title = self._title(title_id, name)
            if self._titles.get(title_id) != title:
                try:
                    self.library.add_title(title)
                except ValueError as error:
                    log.warning("the sheet of %s is refused (%s); it keeps to the name", title_id, error)
                    title = Title(platform="switch", title_id=title_id, name=name)
                    try:
                        self.library.add_title(title)
                    except ValueError:
                        continue
                self._titles[title_id] = title
