# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""What runs behind the server: scan the sources, hash, keep the library in step."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from lantern.server import MemoryLibrary

from lantern_bridge.config import Config
from lantern_bridge.hashing import hash_pending, scan_packages
from lantern_bridge.index import Index
from lantern_bridge.library import Projection

log = logging.getLogger("lantern_bridge.runtime")

#: Seconds between two scans once the queue is empty.
IDLE = 60.0


class Runtime:
    def __init__(self, config: Config, state_dir: Path, library: MemoryLibrary) -> None:
        self.config = config
        self.index = Index(state_dir / "index.sqlite3")
        self.roots = {source.id: source.path for source in config.sources}
        self.projection = Projection(library, self.index, self.roots)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def cycle(self, batch: int = 20) -> int:
        """One pass: scan every source, hash a batch, update the library; returns the files hashed."""
        for source, root in self.roots.items():
            try:
                result = self.index.sync(source, scan_packages(root))
            except OSError as error:
                log.warning("source %s cannot be read: %s", source, error)
                continue
            if result.added or result.changed or result.removed:
                log.info("source %s: +%d ~%d -%d", source, result.added, result.changed, result.removed)
        done = hash_pending(self.index, self.roots, limit=batch)
        self.projection.refresh()
        return done

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                busy = self.cycle() > 0
            except Exception:
                log.exception("a pass failed; trying again")
                busy = False
            self._stop.wait(0 if busy else IDLE)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="lantern-bridge-index", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self.index.close()
