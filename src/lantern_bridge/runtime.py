# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""What runs behind the server: scan the sources, hash, keep the library in step."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from lantern.server import MemoryLibrary
from lantern.server.file_index import Entry, SqliteIndex, SyncResult

from lantern_bridge import __version__
from lantern_bridge.atlas import Atlas
from lantern_bridge.catalog import Browser
from lantern_bridge.compress import Compressor
from lantern_bridge.config import AerofoilSource, Config, FolderSource, OwnfoilSource, ShopSource
from lantern_bridge.hashing import hash_many, scan_packages, workers
from lantern_bridge.history import History
from lantern_bridge.identify import identify
from lantern_bridge.library import Projection
from lantern_bridge.mqtt import Publisher
from lantern_bridge.notify import Notifier
from lantern_bridge.pulse import Pulse
from lantern_bridge.push import Push
from lantern_bridge.releases import Releases
from lantern_bridge.sources import shopkey
from lantern_bridge.sources.aerofoil import AeroFoil
from lantern_bridge.sources.ownfoil import Ownfoil
from lantern_bridge.sources.tinfoil import Shop, UpstreamError
from lantern_bridge.traffic import Traffic
from lantern_bridge.updates import Updates
from lantern_bridge.upstream import AeroRequests, AeroSaves, BridgeLibrary, Cheats, Ledger, Logins
from lantern_bridge.webhook import Webhook

log = logging.getLogger("lantern_bridge.runtime")

#: Seconds between two scans once the queue is empty.
IDLE = 60.0
# While files wait to be hashed, the sources are read again at most this often (a big library takes seconds per pass).
RESCAN_WHILE_BUSY = 30.0
#: Seconds between two reads of a shop's index.
REMOTE_EVERY = 300.0
#: The shortest time between two reads of a shop that said something changed.
CHANGED_EVERY = 10.0
#: Seconds before a file that could not be hashed over the network is tried again.
REMOTE_RETRY = 900.0


@dataclass(slots=True)
class SourceState:
    """What the last pass saw of a source."""

    reachable: bool | None = None
    scanned: float | None = None  # unix time of the last read
    error: str | None = None
    free: int | None = None  # bytes free on the volume of a folder, as of the last read


@dataclass(slots=True)
class Stats:
    """Counters since the process started, for the administration pages."""

    started: float = field(default_factory=time.time)
    hashed_files: int = 0
    hashed_bytes: int = 0
    hashing_seconds: float = 0.0  # time spent hashing, so the rate ignores idle time
    sources: dict[str, SourceState] = field(default_factory=dict)
    #: (source, path) -> (seconds the last hashing took, unix time it finished); since the process started
    timings: dict[tuple[str, str], tuple[float, float]] = field(default_factory=dict)
    #: source -> the packages of a folder its names do not identify, as (path, size) (the last scan)
    ignored: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    #: The last changes the scans found, newest first: (unix time, source, "added|changed|removed", path)
    changes: deque[tuple[float, str, str, str]] = field(default_factory=lambda: deque(maxlen=100))


class Runtime:
    def __init__(self, config: Config, state_dir: Path, library: MemoryLibrary) -> None:
        self.config = config
        self.state_dir = state_dir
        self.index = SqliteIndex(state_dir / "index.sqlite3")
        self.roots = {s.id: s.path for s in config.sources if isinstance(s, FolderSource)}
        self.shops = {s.id: self._shop(s) for s in config.sources if isinstance(s, ShopSource)}
        self.publisher: Publisher | None = None
        self.updates = (
            Updates(notify=lambda latest: self.history.event("update", detail=latest)) if config.update_check else None
        )
        self._updates_thread: threading.Thread | None = None
        self.webhook = Webhook(config.webhook, config.uid, config.name) if config.webhook else None
        self._shop_scanned: dict[str, float] = {}
        self._scanned_at: float | None = None  # None: read the sources on the next pass
        self._changed: set[str] = set()  # shops that said something changed
        self._watchers: list[threading.Thread] = []
        self._remote_failed: dict[tuple[str, str], float] = {}
        self.projection = Projection(library, self.index, self.roots, self.shops, [s.id for s in config.sources])
        self.browser = Browser(self.index, self.projection)
        self.atlas = Atlas(state_dir / "atlas.json") if config.atlas else None
        self.projection.atlas = self.atlas
        self.history = History(state_dir / "activity.sqlite3")
        self.notifier = Notifier(config.notify, config.name) if config.notify else None
        if self.notifier is not None:
            self.history.listeners.append(self.notifier.on_event)
        self.push = Push(state_dir / "push.json", config.name)  # the application gives it the pages' languages
        self.history.listeners.append(self.push.on_event)
        self.releases: Releases | None = None
        if config.homebrew:
            token = os.environ.get(config.github_token_env or "") if config.github_token_env else None
            self.releases = Releases(state_dir, library, list(config.homebrew), token=token or None)
        self._releases_thread: threading.Thread | None = None
        self.compressor: Compressor | None = None  # set by the application: it outlives a reload
        self.pulse = Pulse()  # what the pages' live blocks wait on
        self.history.listeners.append(lambda _event: self.pulse.bump())
        self.traffic = Traffic(history=self.history, pulse=self.pulse)
        self.stats = Stats(sources={source.id: SourceState() for source in config.sources})
        # The first AeroFoil of the configuration keeps the saves, cheats and requests.
        self.ledger: Ledger | None = None
        self.saves: AeroSaves | None = None
        self.requests: AeroRequests | None = None
        self.cheats: Cheats | None = None
        self.aero: AerofoilSource | None = next((s for s in config.sources if isinstance(s, AerofoilSource)), None)
        #: Set by the application: decides an open request (``LanternApp.answer_request``).
        self.answer_request: Callable[[str, str, str | None], object] | None = None
        if self.aero is not None:
            shop = self.shops[self.aero.id]
            if not isinstance(shop, AeroFoil):  # the shop of an AerofoilSource always is
                raise TypeError("an AeroFoil source needs an AeroFoil shop")
            logins = Logins(self.aero, shop)
            self.ledger = Ledger(state_dir / "upstream.sqlite3")
            self.saves = AeroSaves(state_dir / "saves", logins, self.ledger)
            self.requests = AeroRequests(logins, self.ledger)
            if isinstance(library, BridgeLibrary):
                self.cheats = Cheats(library, self.aero.id)
                library.shop = shop if self.aero.user is not None else None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._remote_thread: threading.Thread | None = None
        self._atlas_thread: threading.Thread | None = None

    def _shop(self, source: ShopSource) -> Shop:
        password = os.environ.get(source.password_env) if source.password_env else None
        if source.password_env and password is None:
            log.warning(
                "source %s: the variable %s is not set; the shop is read without a password",
                source.id,
                source.password_env,
            )
        user = source.user if password is not None else None
        hauth = os.environ.get(source.hauth_env) if source.hauth_env else None
        if source.hauth_env and hauth is None:
            log.warning(
                "source %s: the variable %s is not set; no host verification is sent", source.id, source.hauth_env
            )
        if isinstance(source, AerofoilSource):
            key = None
            if source.index_key_file:
                try:
                    key = shopkey.load(source.index_key_file)
                except (OSError, ValueError) as error:
                    log.warning("source %s: the index key cannot be read (%s)", source.id, type(error).__name__)
            return AeroFoil(source.url, user=user, password=password, hauth=hauth, index_key=key)
        if isinstance(source, OwnfoilSource):
            return Ownfoil(source.url, user=user, password=password)
        return Shop(source.url, user=user, password=password, hauth=hauth)

    def open_shop(self, source: ShopSource) -> Shop:
        """A client for a shop of the configuration (the caller closes it)."""
        return self._shop(source)

    def _reach(self, source: str, state: SourceState, error: BaseException | None) -> None:
        """A source answered, or did not; the journal gets the turn from one to the other, not every pass."""
        was = state.reachable
        if error is not None:
            state.reachable, state.error = False, str(error)
            if was is not False:
                # an OS error names the file: only its reason goes to the journal
                why = (error.strerror or type(error).__name__) if isinstance(error, OSError) else str(error)
                self.history.event("source_down", source, why)
            return
        state.reachable, state.scanned, state.error = True, time.time(), None
        if was is False:
            self.history.event("source_up", source)

    def _note(self, source: str, result: SyncResult) -> None:
        """What a scan found goes to the list the pages show (and to the log, as before)."""
        now = time.time()
        for kind, paths in (
            ("added", result.added_paths),
            ("changed", result.changed_paths),
            ("removed", result.removed_paths),
        ):
            for path in paths:
                self.stats.changes.appendleft((now, source, kind, path.rsplit("#", 1)[-1]))
        if self.webhook is not None:
            self.webhook.note(source, result)
        if result.added or result.changed or result.removed:
            log.info("source %s: +%d ~%d -%d", source, result.added, result.changed, result.removed)
            self.history.event("scan", source, f"+{result.added} ~{result.changed} -{result.removed}")

    def _scan_shops(self) -> None:
        for source, shop in self.shops.items():
            waited = time.monotonic() - self._shop_scanned.get(source, -REMOTE_EVERY)
            if waited < (CHANGED_EVERY if source in self._changed else REMOTE_EVERY):
                continue
            self._changed.discard(source)
            state = self.stats.sources.setdefault(source, SourceState())
            self._shop_scanned[source] = time.monotonic()
            try:
                result = self.index.sync(source, shop.scan())
            except UpstreamError as error:
                log.warning("source %s cannot be read: %s", source, error)
                self._reach(source, state, error)
                continue
            self._reach(source, state, None)
            self._note(source, result)
            if source == (self.aero.id if self.aero else None):
                self._mirror(shop)
            if isinstance(shop, Ownfoil):
                try:
                    self.projection.sheets[source] = shop.sheets()
                except UpstreamError as error:
                    log.warning("source %s: the sheets cannot be read: %s", source, error)

    def _mirror(self, shop: Shop) -> None:
        """The AeroFoil's cheats come in, and the fate of the requests made through it."""
        if not isinstance(shop, AeroFoil):
            return
        try:
            if self.cheats is not None:
                self.cheats.refresh(shop)
            if self.requests is not None and self.answer_request is not None:
                self.requests.pull(self.answer_request)
        except UpstreamError as error:
            log.warning("source %s: the cheats cannot be read: %s", shop.url, error)

    def source_changed(self, source: str) -> None:
        """A shop said something changed: read it again soon, not after the regular wait."""
        self._changed.add(source)
        self.wake()

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
                self.stats.timings[(row.source, row.path)] = (time.monotonic() - began, time.time())
                self.stats.hashed_files += 1
                self.stats.hashed_bytes += total
                self.stats.hashing_seconds += time.monotonic() - began
                log.info("source %s: hashed %s", row.source, row.path.rsplit("#", 1)[-1])
                self.pulse.bump()
                return True
        return False

    def cycle(self, batch: int = 20, *, scan: bool = True) -> int:
        """One pass: scan the sources (unless `scan` is off), hash a batch, refresh the library."""
        self._scan_shops()
        for source, root in self.roots.items() if scan else ():
            state = self.stats.sources.setdefault(source, SourceState())
            skipped: list[tuple[str, int]] = []
            try:
                result = self.index.sync(source, scan_packages(root, skipped))
                self.stats.ignored[source] = skipped
            except OSError as error:
                log.warning("source %s cannot be read: %s", source, error)
                self._reach(source, state, error)
                continue
            self._reach(source, state, None)
            try:  # read here, in the scanning thread: a stalled network volume must not stall the pages
                state.free = shutil.disk_usage(root).free
            except OSError:
                state.free = None
            self._note(source, result)
        sizes = {(row.source, row.path): row.size for row in self.index.pending(batch)}
        began = time.monotonic()

        mark = time.monotonic()

        def hashed(source: str, path: str) -> None:
            nonlocal mark
            now = time.monotonic()
            self.stats.timings[(source, path)] = (now - mark, time.time())
            mark = now
            self.stats.hashed_files += 1
            self.stats.hashed_bytes += sizes.get((source, path), 0)
            self.pulse.bump()

        done = hash_many(
            self.index, self.roots, threads=workers(self.config.hash_threads), limit=batch, on_hashed=hashed
        )
        self.stats.hashing_seconds += time.monotonic() - began
        taken = self.projection.fetch_pictures()
        self.projection.refresh()
        self.pulse.bump()
        return done + taken

    def _loop(self) -> None:
        busy = False
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                scan = self._scanned_at is None or not busy or now - self._scanned_at >= RESCAN_WHILE_BUSY
                if scan:
                    self._scanned_at = now
                busy = self.cycle(scan=scan) > 0
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

    def rehash(self, files: list[tuple[str, str]]) -> int:
        """Hash these files again, first in line; returns how many were queued."""
        done = self.index.rehash_files(files)
        for key in files:
            self._remote_failed.pop(key, None)
        self.browser.forget()
        self.wake()
        return done

    def forget_file(self, source: str, path: str) -> None:
        """A file of a folder was deleted from the pages: the index drops that row now, not at the next pass."""
        rows = [r for r in self.index.rows(source) if r.path != path]
        entries = []
        for row in rows:
            identity = identify(row.path)
            entries.append(Entry(row.path, row.size, row.mtime_ns, identity.priority if identity else 0))
        self.index.sync(source, entries)
        self.browser.forget()
        self.wake()

    def rescan(self) -> None:
        """Read every source again now: the shops too, which are otherwise read every few minutes."""
        self._shop_scanned.clear()
        self._scanned_at = None
        self.browser.forget()
        self.wake()

    def wake(self) -> None:
        """Run the next pass now rather than after the idle wait."""
        self._wake.set()

    def start(self) -> None:
        self.history.event("started", detail=__version__)
        if self.updates is not None:
            self._updates_thread = threading.Thread(
                target=self.updates.run, args=(self._stop,), name="lantern-bridge-updates", daemon=True
            )
            self._updates_thread.start()
        if self.config.mqtt is not None:
            self.publisher = Publisher(self, self.config.mqtt)
            self.publisher.start()
        self._thread = threading.Thread(target=self._loop, name="lantern-bridge-index", daemon=True)
        self._thread.start()
        if self.shops:
            self._remote_thread = threading.Thread(target=self._remote_loop, name="lantern-bridge-remote", daemon=True)
            self._remote_thread.start()
        if self.releases is not None:
            self._releases_thread = threading.Thread(
                target=self.releases.run, args=(self._stop,), name="lantern-bridge-homebrew", daemon=True
            )
            self._releases_thread.start()
        if self.atlas is not None:
            self._atlas_thread = threading.Thread(
                target=self.atlas.run, args=(self._stop, self.wake), name="lantern-bridge-atlas", daemon=True
            )
            self._atlas_thread.start()
        for source, shop in self.shops.items():
            if isinstance(shop, Ownfoil):
                watcher = threading.Thread(
                    target=shop.watch,
                    args=(lambda source=source: self.source_changed(source), self._stop),
                    name=f"lantern-bridge-watch-{source}",
                    daemon=True,
                )
                watcher.start()
                self._watchers.append(watcher)

    def stop(self) -> None:
        self.halt()
        self.close()

    def halt(self) -> None:
        """Stops what runs by itself (the passes, the broker, the notifications) and keeps the stores open, so a
        request still being answered can read them: a settings reload halts the old runtime, lets its last requests
        finish, then closes it."""
        if self.publisher is not None:
            self.publisher.stop()
        if self.webhook is not None:
            self.webhook.close()
        if self.notifier is not None:
            self.notifier.close()
        if self.updates is not None:
            self.updates.close()
        self._stop.set()
        self._wake.set()
        for thread in (
            self._thread,
            self._remote_thread,
            self._atlas_thread,
            self._updates_thread,
            self._releases_thread,
            *self._watchers,
        ):
            if thread is not None:
                thread.join(timeout=10)

    def close(self) -> None:
        for shop in self.shops.values():
            shop.close()
        if self.atlas is not None:
            self.atlas.close()
        if self.releases is not None:
            self.releases.close()
        self.push.close()
        self.traffic.close()
        self.history.close()
        self.index.close()
        if self.ledger is not None:
            self.ledger.close()
