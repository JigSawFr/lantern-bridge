# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""What runs behind the server: scan the sources, hash, keep the library in step."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from lantern.server import MemoryLibrary

from lantern_bridge.config import Config, FolderSource, TinfoilSource
from lantern_bridge.hashing import hash_pending, scan_packages
from lantern_bridge.index import Index
from lantern_bridge.library import Projection
from lantern_bridge.sources.tinfoil import Shop, UpstreamError

log = logging.getLogger("lantern_bridge.runtime")

#: Seconds between two scans once the queue is empty.
IDLE = 60.0
#: Seconds between two reads of a shop's index.
REMOTE_EVERY = 300.0
#: Seconds before a file that could not be hashed over the network is tried again.
REMOTE_RETRY = 900.0


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
        self.roots = {s.id: s.path for s in config.sources if isinstance(s, FolderSource)}
        self.shops = {s.id: self._shop(s) for s in config.sources if isinstance(s, TinfoilSource)}
        self._shop_scanned: dict[str, float] = {}
        self._remote_failed: dict[tuple[str, str], float] = {}
        self.projection = Projection(library, self.index, self.roots, self.shops, [s.id for s in config.sources])
        self.stats = Stats(sources={source.id: SourceState() for source in config.sources})
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._remote_thread: threading.Thread | None = None

    def _shop(self, source: TinfoilSource) -> Shop:
        password = os.environ.get(source.password_env) if source.password_env else None
        if source.password_env and password is None:
            log.warning(
                "source %s: the variable %s is not set; the shop is read without a password",
                source.id,
                source.password_env,
            )
        return Shop(source.url, user=source.user if password is not None else None, password=password)

    def _scan_shops(self) -> None:
        for source, shop in self.shops.items():
            if time.monotonic() - self._shop_scanned.get(source, -REMOTE_EVERY) < REMOTE_EVERY:
                continue
            state = self.stats.sources.setdefault(source, SourceState())
            self._shop_scanned[source] = time.monotonic()
            try:
                result = self.index.sync(source, shop.scan())
            except UpstreamError as error:
                log.warning("source %s cannot be read: %s", source, error)
                state.reachable, state.error = False, str(error)
                continue
            state.reachable, state.scanned, state.error = True, time.time(), None
            if result.added or result.changed or result.removed:
                log.info("source %s: +%d ~%d -%d", source, result.added, result.changed, result.removed)

    def hash_remote(self) -> bool:
        """Hash one file of a shop by reading it (at the configured rate); True when it hashed one."""
        now = time.monotonic()
        for row in self.index.pending(20, tuple(self.shops)):
            if self._remote_failed.get((row.source, row.path), 0) > now:
                continue
            began = time.monotonic()
            digest, total = hashlib.sha256(), 0
            limit = self.config.remote_rate * (1 << 20)
            try:
                for chunk in self.shops[row.source].stream(row.path):
                    if self._stop.is_set():
                        return False
                    digest.update(chunk)
                    total += len(chunk)
                    if total > row.size:
                        raise UpstreamError("the file is larger than the shop said")
                    if limit:  # the pace is kept on the whole file, so a slow link is never slowed further
                        ahead = total / limit - (time.monotonic() - began)
                        if ahead > 0:
                            self._stop.wait(ahead)
                if total != row.size:
                    raise UpstreamError("the file is smaller than the shop said")
            except UpstreamError as error:
                log.warning("source %s: %s cannot be hashed: %s", row.source, row.path.rsplit("#", 1)[-1], error)
                self._remote_failed[(row.source, row.path)] = time.monotonic() + REMOTE_RETRY
                continue
            if self.index.set_hash(row, digest.hexdigest()):
                self.stats.hashed_files += 1
                self.stats.hashed_bytes += total
                self.stats.hashing_seconds += time.monotonic() - began
                log.info("source %s: hashed %s", row.source, row.path.rsplit("#", 1)[-1])
                return True
        return False

    def cycle(self, batch: int = 20) -> int:
        """One pass: scan every source, hash a batch, update the library; returns the files hashed."""
        self._scan_shops()
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

    def _remote_loop(self) -> None:
        """Files of the shops are hashed apart from the local ones, one at a time: a big file takes hours."""
        while not self._stop.is_set():
            try:
                busy = self.hash_remote()
            except Exception:
                log.exception("hashing over the network failed; trying again")
                busy = False
            if not busy:
                self._stop.wait(30)

    def wake(self) -> None:
        """Run the next pass now rather than after the idle wait."""
        self._wake.set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="lantern-bridge-index", daemon=True)
        self._thread.start()
        if self.shops:
            self._remote_thread = threading.Thread(target=self._remote_loop, name="lantern-bridge-remote", daemon=True)
            self._remote_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for thread in (self._thread, self._remote_thread):
            if thread is not None:
                thread.join(timeout=10)
        for shop in self.shops.values():
            shop.close()
        self.index.close()
