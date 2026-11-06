# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The library the server shows: the hashed files of the index, as Lantern apps.

The SDK's ``MemoryLibrary`` holds and serves the catalogue (and its journal);
this module only keeps it equal to the index. A file appears once it is hashed,
disappears when it is gone or changed, and is never hashed again here.
"""

from __future__ import annotations

import logging
from pathlib import Path

from lantern._generated.models import App
from lantern.server.files import FileRef
from lantern.server.library import MemoryLibrary

from lantern_bridge.identify import identify
from lantern_bridge.index import Index, Row

log = logging.getLogger("lantern_bridge.library")


class Projection:
    """Keeps a ``MemoryLibrary`` equal to the hashed rows of an ``Index``."""

    def __init__(self, library: MemoryLibrary, index: Index, roots: dict[str, Path]) -> None:
        self.library = library
        self.index = index
        self.roots = roots
        self._shown: dict[tuple[str, str], tuple[Row, str]] = {}

    def refresh(self) -> tuple[int, int]:
        """Add what became listable, remove what is gone; returns (added, removed)."""
        rows = {(r.source, r.path): r for r in self.index.hashed()}
        removed = 0
        for key in list(self._shown):
            row, app_id = self._shown[key]
            if rows.get(key) != row:
                del self._shown[key]
                removed += 1
                if not any(other == app_id for _, other in self._shown.values()):
                    self.library.remove_app(app_id)
        added = 0
        for key, row in rows.items():
            if key in self._shown or row.sha256 is None:
                continue
            root = self.roots.get(row.source)
            identity = identify(row.path)
            if root is None or identity is None:
                continue
            ref = FileRef(str(root / row.path), row.size, row.sha256)
            app = App(
                id="",
                platform="switch",
                title_id=identity.title_id,
                app_id=identity.app_id,
                type=identity.type,
                version_code=identity.version_code,
                name=identity.name,
                size=row.size,
                format=identity.format,
                sha256=row.sha256,
                file="",
            )
            app.id = "a_" + row.sha256[:12]
            if any(other == app.id for _, other in self._shown.values()):
                self._shown[key] = (row, app.id)  # the same bytes under another path: one app
                continue
            try:
                self.library.add_app(app, ref)
            except ValueError as error:
                log.warning("not listed: %s/%s (%s)", row.source, row.path, error)
                continue
            self._shown[key] = (row, app.id)
            added += 1
        return added, removed
