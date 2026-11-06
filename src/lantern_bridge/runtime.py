# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""What runs behind the server: scan the sources, hash, keep the library in step."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from lantern.server import MemoryLibrary

from lantern_bridge.config import Config
from lantern_bridge.hashing import hash_pending, scan_packages
from lantern_bridge.index import Index
from lantern_bridge.library import Projection

log = logging.getLogger("lantern_bridge.runtime")

#: Seconds between two scans once the queue is empty.
IDLE = 60.0


@dataclass(slots=True)
class SourceState:
    """What the last pass saw of a source."""

    reachable: bool | None = None
    scanned: float | None = None  # unix time of the last read
    error: str | None = None


@dataclass(slots=True)
class Stats:
    """Counters since the process started, for the administration pages."""

    started: float = field(default_factory=time.time)
    hashed_files: int = 0
    hashed_bytes: int = 0
    hashing_seconds: float = 0.0  # time spent hashing, so the rate ignores idle time
    sources: dict[str, SourceState] = field(default_factory=dict)


class Runtime:
    def __init__(self, config: Config, state_dir: Path, library: MemoryLibrary) -> None:
        self.config = config
        self.index = Index(state_dir / "index.sqlite3")
        self.roots = {source.id: source.path for source in config.sources}
        self.projection = Projection(library, self.index, self.roots)
        self.stats = Stats(sources={source.id: SourceState() for source in config.sources})
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def cycle(self, batch: int = 20) -> int:
        """One pass: scan every source, hash a batch, update the library; returns the files hashed."""
        for source, root in self.roots.items():
            state = self.stats.sources.setdefault(source, SourceState())
            try:
                result = self.index.sync(source, scan_packages(root))
            except OSError as error:
                log.warning("source %s cannot be read: %s", source, error)
                state.reachable, state.error = False, str(error)
                continue
            state.reachable, state.scanned, state.error = True, time.time(), None
            if result.added or result.changed or result.removed:
                log.info("source %s: +%d ~%d -%d", source, result.added, result.changed, result.removed)
        sizes = {(row.source, row.path): row.size for row in self.index.pending(batch)}
        began = time.monotonic()

        def hashed(source: str, path: str) -> None:
            self.stats.hashed_files += 1
            self.stats.hashed_bytes += sizes.get((source, path), 0)

        done = hash_pending(self.index, self.roots, limit=batch, on_hashed=hashed)
        self.stats.hashing_seconds += time.monotonic() - began
        self.projection.refresh()
        return done

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                busy = self.cycle() > 0
            except Exception:
                log.exception("a pass failed; trying again")
                busy = False
            if not busy:
                self._wake.wait(IDLE)
            self._wake.clear()

    def wake(self) -> None:
        """Run the next pass now rather than after the idle wait."""
        self._wake.set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="lantern-bridge-index", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self.index.close()
