# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The library the server shows: the hashed files of the index, as Lantern apps.

The SDK's ``MemoryLibrary`` holds and serves the catalogue (and its journal);
this module only keeps it equal to the index. A file appears once it is hashed,
disappears when it is gone or changed, and is never hashed again here.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from pathlib import Path

from lantern._generated.models import App, Title
from lantern.server.files import FileRef
from lantern.server.library import MemoryLibrary

from lantern_bridge.identify import Identity, identify
from lantern_bridge.index import Index, Row
from lantern_bridge.sources.tinfoil import Shop

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
        self._titles: dict[str, str] = {}

    def served(self) -> tuple[int, int]:
        """How many apps and how many titles the library offers now."""
        return len(self._shown), len({identity.title_id for _, identity in self._shown.values()})

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

    def _refresh_titles(self) -> None:
        """One title sheet for each title id an app belongs to (SPEC.md § 7.1): the base game names it."""
        wanted: dict[str, str] = {}
        for _, identity in sorted(self._shown.values(), key=lambda v: v[1].priority, reverse=True):
            wanted[identity.title_id] = identity.name  # the base comes last, and wins
        for title_id in set(self._titles) - set(wanted):
            self.library.remove_title("switch", title_id)
            del self._titles[title_id]
        for title_id, name in wanted.items():
            if self._titles.get(title_id) != name:
                try:
                    self.library.add_title(Title(platform="switch", title_id=title_id, name=name))
                except ValueError as error:
                    log.warning("no sheet for %s (%s)", title_id, error)
                    continue
                self._titles[title_id] = name
