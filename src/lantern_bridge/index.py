# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The index of the files and their SHA-256, in one SQLite file.

The key is the source and the path. The validators are the size and the
modification time: a hash is computed again only when one of them changes.
A file is **not listed until it is hashed** (``hashed``). The schema is
SQLite's ``user_version``: a newer file is refused and never written, and a
column or table this version does not know is left alone.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

SCHEMA = 1

_TABLES = """
CREATE TABLE files (
    source TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, path)
);
CREATE INDEX files_pending ON files (priority, size) WHERE sha256 IS NULL;
"""


class IndexSchemaError(RuntimeError):
    """The index comes from a newer version; it is left exactly as it is."""


@dataclass(frozen=True, slots=True)
class Entry:
    """A file seen on disk: its path inside the source and its validators."""

    path: str
    size: int
    mtime_ns: int
    priority: int = 0


@dataclass(frozen=True, slots=True)
class Row:
    source: str
    path: str
    size: int
    mtime_ns: int
    sha256: str | None


@dataclass(frozen=True, slots=True)
class SyncResult:
    added: int
    changed: int
    removed: int


class Index:
    """Thread-safe: one connection, one lock, writes inside ``BEGIN IMMEDIATE``."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        name = os.fspath(path)
        if name != ":memory:" and not Path(name).exists():
            Path(name).parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(name, os.O_WRONLY | os.O_CREAT, 0o600))
        self._lock = threading.RLock()
        self._db = sqlite3.connect(name, check_same_thread=False, isolation_level=None)
        try:
            self._open()
        except BaseException:
            self._db.close()
            raise

    def _open(self) -> None:
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA:
            raise IndexSchemaError(
                f"This index is of schema {version}, newer than the {SCHEMA} this version knows: it is left as it is."
            )
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._db.execute("PRAGMA journal_mode = WAL")
        if version == 0:
            with self._transaction():
                if self._db.execute("SELECT 1 FROM sqlite_master WHERE name = 'files'").fetchone() is None:
                    for statement in _TABLES.split(";"):
                        if statement.strip():
                            self._db.execute(statement)
                self._db.execute(f"PRAGMA user_version = {SCHEMA}")

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> Index:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def sync(self, source: str, entries: Iterable[Entry]) -> SyncResult:
        """Make the source's rows match what is on disk.

        A new file is pending, a file whose size or time changed loses its hash,
        and a file that is gone disappears.
        """
        seen = {entry.path: entry for entry in entries}
        added = changed = 0
        with self._transaction() as db:
            known = {
                path: (size, mtime)
                for path, size, mtime in db.execute(
                    "SELECT path, size, mtime_ns FROM files WHERE source = ?", (source,)
                )
            }
            for path, entry in seen.items():
                if path not in known:
                    db.execute(
                        "INSERT INTO files (source, path, size, mtime_ns, priority) VALUES (?, ?, ?, ?, ?)",
                        (source, path, entry.size, entry.mtime_ns, entry.priority),
                    )
                    added += 1
                elif known[path] != (entry.size, entry.mtime_ns):
                    db.execute(
                        "UPDATE files SET size = ?, mtime_ns = ?, sha256 = NULL, priority = ? "
                        "WHERE source = ? AND path = ?",
                        (entry.size, entry.mtime_ns, entry.priority, source, path),
                    )
                    changed += 1
            gone = [(source, path) for path in known if path not in seen]
            db.executemany("DELETE FROM files WHERE source = ? AND path = ?", gone)
        return SyncResult(added=added, changed=changed, removed=len(gone))

    def pending(self, limit: int = 100) -> list[Row]:
        """Files to hash: the lowest priority number first, then the smallest."""
        with self._lock:
            rows = self._db.execute(
                "SELECT source, path, size, mtime_ns, sha256 FROM files WHERE sha256 IS NULL "
                "ORDER BY priority, size, source, path LIMIT ?",
                (limit,),
            ).fetchall()
        return [Row(*row) for row in rows]

    def set_hash(self, row: Row, sha256: str) -> bool:
        """Record a hash, only if the validators are still the ones it was computed under."""
        with self._transaction() as db:
            cursor = db.execute(
                "UPDATE files SET sha256 = ? WHERE source = ? AND path = ? AND size = ? AND mtime_ns = ?",
                (sha256, row.source, row.path, row.size, row.mtime_ns),
            )
            return cursor.rowcount == 1

    def hashed(self, source: str | None = None) -> list[Row]:
        """The files that can be listed: those with a hash."""
        query = "SELECT source, path, size, mtime_ns, sha256 FROM files WHERE sha256 IS NOT NULL"
        args: tuple[str, ...] = ()
        if source is not None:
            query += " AND source = ?"
            args = (source,)
        with self._lock:
            return [Row(*row) for row in self._db.execute(query + " ORDER BY source, path", args)]

    def counts(self) -> dict[str, int]:
        with self._lock:
            done, todo = self._db.execute("SELECT COUNT(sha256), COUNT(*) - COUNT(sha256) FROM files").fetchone()
        return {"hashed": int(done), "pending": int(todo)}

    def pending_bytes(self) -> int:
        """The size of the files still to hash."""
        with self._lock:
            (total,) = self._db.execute("SELECT COALESCE(SUM(size), 0) FROM files WHERE sha256 IS NULL").fetchone()
        return int(total)

    def counts_by_source(self) -> dict[str, dict[str, int]]:
        """Per source: how many files are hashed and how many are waiting."""
        with self._lock:
            rows = self._db.execute(
                "SELECT source, COUNT(sha256), COUNT(*) - COUNT(sha256) FROM files GROUP BY source ORDER BY source"
            ).fetchall()
        return {source: {"hashed": int(done), "pending": int(todo)} for source, done, todo in rows}

    def rehash(self, source: str | None = None) -> int:
        """Forget the hashes of a source (or of all), so they are computed again; returns how many."""
        query = "UPDATE files SET sha256 = NULL WHERE sha256 IS NOT NULL"
        args: tuple[str, ...] = ()
        if source is not None:
            query += " AND source = ?"
            args = (source,)
        with self._transaction() as db:
            return db.execute(query, args).rowcount
