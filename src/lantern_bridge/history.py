# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""What the administration pages remember of the traffic across restarts: totals by day and the machines seen.

Counters only (requests, bytes sent, errors) by day and by machine; no path, no query, no token, no body.
A day older than `KEEP_DAYS` is dropped. The file is its own (``activity.sqlite3``) and carries its schema
number, so a newer one is left alone and the pages simply start from nothing.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = 1
KEEP_DAYS = 90
MAX_MACHINES = 200


@dataclass(frozen=True, slots=True)
class Delta:
    """What one machine did since the last flush."""

    key: str  # "device:<id>" for a paired device, "addr:<address>" otherwise
    label: str  # the device's name and account, or empty
    address: str
    agent: str
    first: float
    last: float
    requests: int
    sent: int
    errors: int


@dataclass(frozen=True, slots=True)
class Day:
    day: str
    requests: int
    sent: int
    errors: int


@dataclass(frozen=True, slots=True)
class Seen:
    key: str
    label: str
    address: str
    agent: str
    first: float
    last: float
    requests: int
    sent: int


def today(now: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, UTC).strftime("%Y-%m-%d")


class History:
    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        try:
            db = sqlite3.connect(path, check_same_thread=False)
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA:  # written by a newer bridge: left alone, nothing is read or written
                log.warning("%s is of a newer version: the activity history is off", path.name)
                db.close()
                return
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS days (day TEXT, key TEXT, requests INTEGER, sent INTEGER, errors INTEGER,
                    PRIMARY KEY (day, key));
                CREATE TABLE IF NOT EXISTS machines (key TEXT PRIMARY KEY, label TEXT, address TEXT, agent TEXT,
                    first REAL, last REAL, requests INTEGER, sent INTEGER);
                """
            )
            db.execute(f"PRAGMA user_version = {SCHEMA}")
            self._db = db
            self._prune()
        except sqlite3.Error as error:
            log.warning("the activity history is off: %s", error)

    @property
    def usable(self) -> bool:
        return self._db is not None

    def _prune(self) -> None:
        if self._db is None:
            return
        cutoff = today(time.time() - KEEP_DAYS * 86400)
        with self._lock, self._db:
            self._db.execute("DELETE FROM days WHERE day < ?", (cutoff,))
            self._db.execute(
                "DELETE FROM machines WHERE key NOT IN (SELECT key FROM machines ORDER BY last DESC LIMIT ?)",
                (MAX_MACHINES,),
            )

    def add(self, deltas: Iterable[Delta], day: str | None = None) -> None:
        if self._db is None:
            return
        day = day or today()
        try:
            with self._lock, self._db:
                for d in deltas:
                    self._db.execute(
                        "INSERT INTO days VALUES (?,?,?,?,?) ON CONFLICT(day, key) DO UPDATE SET "
                        "requests = requests + excluded.requests, sent = sent + excluded.sent, "
                        "errors = errors + excluded.errors",
                        (day, d.key, d.requests, d.sent, d.errors),
                    )
                    self._db.execute(
                        "INSERT INTO machines VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                        "label = excluded.label, address = excluded.address, "
                        "agent = CASE WHEN excluded.agent = '' THEN agent ELSE excluded.agent END, "
                        "last = excluded.last, requests = requests + excluded.requests, sent = sent + excluded.sent",
                        (d.key, d.label, d.address, d.agent, d.first, d.last, d.requests, d.sent),
                    )
        except sqlite3.Error as error:
            log.warning("the activity history could not be written: %s", error)

    def days(self, count: int = 14) -> list[Day]:
        """The last `count` days, oldest first, the quiet ones as zeros."""
        if self._db is None:
            return []
        first = datetime.now(UTC).date() - timedelta(days=count - 1)
        with self._lock:
            rows = self._db.execute(
                "SELECT day, SUM(requests), SUM(sent), SUM(errors) FROM days WHERE day >= ? GROUP BY day",
                (first.isoformat(),),
            ).fetchall()
        known = {r[0]: Day(r[0], r[1], r[2], r[3]) for r in rows}
        out = []
        for i in range(count):
            name = (first + timedelta(days=i)).isoformat()
            out.append(known.get(name, Day(name, 0, 0, 0)))
        return out

    def machines(self) -> list[Seen]:
        if self._db is None:
            return []
        with self._lock:
            rows = self._db.execute("SELECT * FROM machines ORDER BY last DESC LIMIT ?", (MAX_MACHINES,)).fetchall()
        return [Seen(*r) for r in rows]

    def forget(self, key: str) -> None:
        if self._db is not None:
            with self._lock, self._db:
                self._db.execute("DELETE FROM machines WHERE key = ?", (key,))

    def close(self) -> None:
        if self._db is not None:
            with self._lock:
                self._db.close()
                self._db = None
