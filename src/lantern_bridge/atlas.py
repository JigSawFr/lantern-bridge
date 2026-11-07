# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""Atlas: the open data on the games of each platform (https://atlas.boreale.dev), for the sheets of the titles.

One public file, ``platforms/switch/games.json`` of the ``data`` branch (CC0): per title id the name, the
developers, publishers, genres, series and modes Wikidata gives, and Steam's rating when the game is on Steam.
The bridge reads it once a day, keeps it in its state directory and works from that copy; without the
network, or with the setting off, nothing changes but the sheets stay as their sources make them.

The file has a ``schema``: a newer one is treated as absent (nothing is read from it, nothing is written
back), as every format of the house.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("lantern_bridge.atlas")

DEFAULT_URL = "https://raw.githubusercontent.com/JigSawFr/PlayInsights/data/platforms/switch/games.json"
SCHEMA = 1  # the games file's schema this version reads
CACHE_SCHEMA = 1
MAX_BYTES = 16 << 20
REFRESH = 24 * 3600.0
RETRY = 3600.0


@dataclass(frozen=True, slots=True)
class Entry:
    """What Atlas says of a game."""

    name: str
    developers: tuple[str, ...] = ()
    publishers: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    series: tuple[str, ...] = ()
    modes: tuple[str, ...] = ()
    steam_app: int | None = None
    positive: int = 0
    negative: int = 0

    @property
    def rating(self) -> int | None:
        """Steam's share of positive reviews, in percent, when there are enough to say."""
        total = self.positive + self.negative
        return round(100 * self.positive / total) if total >= 10 else None


#: Lantern's fixed list of genres (SPEC.md § 7.1), and the words of Wikidata's labels that say each one.
_GENRES = (
    ("platformer", ("platform",)),
    ("rpg", ("role-playing", "rpg", "jrpg")),
    ("shooter", ("shooter", "shoot 'em up", "shmup")),
    ("fighting", ("fighting", "beat 'em up")),
    ("racing", ("racing", "driving")),
    ("sports", ("sport", "football", "soccer", "golf", "basketball", "tennis", "baseball")),
    ("strategy", ("strategy", "tactic", "tower defense", "4x")),
    ("simulation", ("simulation", "simulator", "life", "farming", "management", "city-building")),
    ("puzzle", ("puzzle",)),
    ("music", ("music", "rhythm")),
    ("party", ("party",)),
    ("education", ("educational", "edutainment")),
    ("adventure", ("adventure", "visual novel", "point-and-click")),
    ("action", ("action", "hack and slash", "roguelike", "metroidvania", "stealth", "survival")),
)


def genres_of(labels: tuple[str, ...]) -> list[str]:
    """Lantern's genres for Wikidata's labels (a label that says none of them says nothing)."""
    found: list[str] = []
    for genre, words in _GENRES:
        if genre not in found and any(word in label.lower() for label in labels for word in words):
            found.append(genre)
    return found


def _labels(value: object) -> tuple[str, ...]:
    """The labels of Atlas's ``[[qid, label], …]`` lists."""
    out: list[str] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, list) and len(item) == 2 and isinstance(item[1], str) and item[1]:
            out.append(item[1])
    return tuple(out)


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _entry(raw: object) -> Entry | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not raw["name"]:
        return None
    steam = raw.get("steam")
    steam = steam if isinstance(steam, dict) else {}
    app = steam.get("app")
    return Entry(
        name=raw["name"],
        developers=_labels(raw.get("developers")),
        publishers=_labels(raw.get("publishers")),
        genres=_labels(raw.get("genres")),
        series=_labels(raw.get("series")),
        modes=_labels(raw.get("modes")),
        steam_app=app if isinstance(app, int) and not isinstance(app, bool) else None,
        positive=_count(steam.get("positive")),
        negative=_count(steam.get("negative")),
    )


def parse(document: object) -> dict[str, Entry] | None:
    """The games of a downloaded file; None when it is not one this version reads (another shape or a newer schema)."""
    if not isinstance(document, dict):
        return None
    schema = document.get("schema")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema > SCHEMA or schema < 1:
        return None
    games = document.get("games")
    if not isinstance(games, dict):
        return None
    out: dict[str, Entry] = {}
    for title_id, raw in games.items():
        entry = _entry(raw)
        if entry is not None and isinstance(title_id, str) and len(title_id) == 16:
            out[title_id.upper()] = entry
    return out


class Atlas:
    """The copy of Atlas's games the bridge works from, and the way it keeps it fresh."""

    def __init__(self, cache: Path, url: str = DEFAULT_URL, *, client: httpx.Client | None = None) -> None:
        self.cache, self.url = cache, url
        self._client = client or httpx.Client(
            timeout=30.0, follow_redirects=True, headers={"User-Agent": "lantern-bridge"}
        )
        self._games: dict[str, Entry] = {}
        self.fetched: float | None = None
        self.error: str | None = None
        self._etag: str | None = None
        self._lock = threading.Lock()
        self._load()

    def __len__(self) -> int:
        return len(self._games)

    def get(self, title_id: str) -> Entry | None:
        return self._games.get(title_id.upper())

    # -- the copy on disk ----------------------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("schema") != CACHE_SCHEMA:
            return  # another version's file: not read, and replaced only by a successful fetch of our own
        games = parse(data.get("atlas"))
        if games is None:
            return
        fetched = data.get("fetched")
        self._games, self._etag = games, data.get("etag") if isinstance(data.get("etag"), str) else None
        self.fetched = float(fetched) if isinstance(fetched, (int, float)) else None

    def _save(self, document: dict[str, Any]) -> None:
        body = {"schema": CACHE_SCHEMA, "fetched": self.fetched, "etag": self._etag, "atlas": document}
        tmp = self.cache.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.cache)

    # -- the network ---------------------------------------------------------------------------

    def due(self) -> bool:
        return self.fetched is None or time.time() - self.fetched > REFRESH

    def refresh(self) -> bool:
        """Fetch the file again (a conditional request); True when the games changed. Never raises."""
        headers = {"If-None-Match": self._etag} if self._etag and self._games else {}
        try:
            with self._client.stream("GET", self.url, headers=headers) as reply:
                if reply.status_code == 304:
                    self.fetched, self.error = time.time(), None
                    return False
                if reply.status_code != 200:
                    raise ValueError(f"answered {reply.status_code}")
                received = bytearray()
                for chunk in reply.iter_bytes():
                    received += chunk
                    if len(received) > MAX_BYTES:
                        raise ValueError("the file is larger than 16 MiB")
                etag = reply.headers.get("etag")
            document = json.loads(received)
            games = parse(document)
            if games is None:
                raise ValueError("a file this version does not read (another shape, or a newer schema)")
        except (httpx.HTTPError, ValueError, OSError) as error:
            self.error = f"{type(error).__name__}: {error}"[:200]
            log.warning("Atlas cannot be fetched (%s); the copy kept is used", self.error)
            return False
        with self._lock:
            self._games, self._etag, self.fetched, self.error = games, etag, time.time(), None
        try:
            self._save(document)
        except OSError as error:
            log.warning("Atlas's copy cannot be written (%s)", error)
        log.info("Atlas: %d games", len(games))
        return True

    def run(self, stop: threading.Event, changed: Any = None) -> None:
        """The background loop: fetch when the copy is old, try again in an hour after a failure."""
        while not stop.is_set():
            wait = RETRY
            if self.due():
                if self.refresh() and changed is not None:
                    changed()
                wait = RETRY if self.error else REFRESH
            else:
                wait = max(60.0, REFRESH - (time.time() - (self.fetched or 0)))
            stop.wait(wait)

    def close(self) -> None:
        self._client.close()
