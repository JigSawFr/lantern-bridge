# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""What the administration pages remember across restarts: the traffic by day, the machines seen, the journal.

The traffic is counters only (requests, bytes sent, errors) by day and by machine; no path, no query, no token,
no body. The journal is the events worth a line (a scan that found changes, a source gone and back, a device
paired, a setting saved...): a kind, a subject (a source id, an account) and a short detail, never a path or a
title. A day older than `KEEP_DAYS` is dropped. The file is its own (``activity.sqlite3``) and carries its schema
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
MAX_EVENTS = 1000


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
class Event:
    ts: float
    kind: str  # started, scan, source_down, source_up, update, settings, paired, revoked
    subject: str  # a source id or an account, or empty
    detail: str  # short; never a path or a title


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
                CREATE TABLE IF NOT EXISTS events (ts REAL, kind TEXT, subject TEXT, detail TEXT);
                CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
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
            self._db.execute("DELETE FROM events WHERE ts < ?", (time.time() - KEEP_DAYS * 86400,))
            self._db.execute(
                "DELETE FROM events WHERE rowid NOT IN (SELECT rowid FROM events ORDER BY ts DESC LIMIT ?)",
                (MAX_EVENTS,),
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

    def event(self, kind: str, subject: str = "", detail: str = "", now: float | None = None) -> None:
        """Adds a line to the journal (nothing happens when the file is off)."""
        if self._db is None:
            return
        try:
            with self._lock, self._db:
                self._db.execute(
                    "INSERT INTO events VALUES (?,?,?,?)",
                    (time.time() if now is None else now, kind[:32], subject[:64], detail[:120]),
                )
        except sqlite3.Error as error:
            log.warning("the journal could not be written: %s", error)

    def events(self, limit: int = 200, kind: str = "") -> list[Event]:
        """The latest events, newest first."""
        if self._db is None:
            return []
        with self._lock:
            if kind:
                rows = self._db.execute(
                    "SELECT ts, kind, subject, detail FROM events WHERE kind = ? ORDER BY ts DESC, rowid DESC LIMIT ?",
                    (kind, limit),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT ts, kind, subject, detail FROM events ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,)
                ).fetchall()
        return [Event(*r) for r in rows]

    def forget(self, key: str) -> None:
        if self._db is not None:
            with self._lock, self._db:
                self._db.execute("DELETE FROM machines WHERE key = ?", (key,))

    def close(self) -> None:
        if self._db is not None:
            with self._lock:
                self._db.close()
                self._db = None
