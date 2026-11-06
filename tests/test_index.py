# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import threading
from pathlib import Path

import pytest

from lantern_bridge.hashing import hash_pending, scan, sha256_file
from lantern_bridge.index import SCHEMA, Entry, Index, IndexSchemaError


def test_new_files_are_pending_then_listed_once_hashed(tmp_path: Path) -> None:
    (tmp_path / "a.nsp").write_bytes(b"alpha")
    with Index(":memory:") as index:
        assert index.sync("games", scan(tmp_path)).added == 1
        assert index.hashed() == []
        assert index.counts() == {"hashed": 0, "pending": 1}
        assert hash_pending(index, {"games": tmp_path}) == 1
        [row] = index.hashed()
        assert row.sha256 == hashlib.sha256(b"alpha").hexdigest()


def test_a_hash_is_recomputed_only_when_a_validator_changes(tmp_path: Path) -> None:
    file = tmp_path / "a.nsp"
    file.write_bytes(b"alpha")
    with Index(":memory:") as index:
        index.sync("g", scan(tmp_path))
        hash_pending(index, {"g": tmp_path})
        result = index.sync("g", scan(tmp_path))
        assert (result.added, result.changed, result.removed) == (0, 0, 0)
        assert index.counts()["pending"] == 0
        file.write_bytes(b"alphabet")
        assert index.sync("g", scan(tmp_path)).changed == 1
        assert index.hashed() == []
        hash_pending(index, {"g": tmp_path})
        assert index.hashed()[0].sha256 == hashlib.sha256(b"alphabet").hexdigest()


def test_removed_files_disappear(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"1")
    (tmp_path / "b").write_bytes(b"2")
    with Index(":memory:") as index:
        index.sync("g", scan(tmp_path))
        (tmp_path / "a").unlink()
        assert index.sync("g", scan(tmp_path)).removed == 1
        assert index.counts()["pending"] == 1


def test_sources_do_not_touch_each_other() -> None:
    with Index(":memory:") as index:
        index.sync("one", [Entry("x", 1, 1)])
        index.sync("two", [])
        assert index.counts()["pending"] == 1


def test_pending_goes_by_priority_then_smallest() -> None:
    with Index(":memory:") as index:
        index.sync("g", [Entry("big-base", 900, 1, 0), Entry("small-dlc", 5, 1, 2), Entry("small-base", 10, 1, 0)])
        assert [r.path for r in index.pending()] == ["small-base", "big-base", "small-dlc"]


def test_a_hash_computed_under_old_validators_is_dropped() -> None:
    with Index(":memory:") as index:
        index.sync("g", [Entry("a", 1, 1)])
        [row] = index.pending()
        index.sync("g", [Entry("a", 2, 2)])
        assert index.set_hash(row, "0" * 64) is False
        assert index.hashed() == []


def test_symbolic_links_are_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"x")
    root = tmp_path / "root"
    root.mkdir()
    (root / "ok").write_bytes(b"y")
    try:
        (root / "link").symlink_to(outside / "secret")
        (root / "dir").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("no symbolic links here")
    assert [e.path for e in scan(root)] == ["ok"]


def test_the_index_survives_a_restart_and_is_private(tmp_path: Path) -> None:
    path = tmp_path / "state" / "index.sqlite3"
    with Index(path) as index:
        index.sync("g", [Entry("a", 1, 1)])
    with Index(path) as index:
        assert index.counts()["pending"] == 1
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_newer_schema_is_refused_and_untouched(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE future (x)")
    db.execute(f"PRAGMA user_version = {SCHEMA + 1}")
    db.commit()
    db.close()
    before = path.read_bytes()
    with pytest.raises(IndexSchemaError):
        Index(path)
    assert path.read_bytes() == before


def test_unknown_columns_and_tables_are_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite3"
    with Index(path) as index:
        index.sync("g", [Entry("a", 1, 1)])
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE files ADD COLUMN extra TEXT DEFAULT 'kept'")
    db.execute("CREATE TABLE other (x)")
    db.commit()
    db.close()
    with Index(path) as index:
        index.sync("g", [Entry("a", 1, 1), Entry("b", 1, 1)])
    db = sqlite3.connect(path)
    assert db.execute("SELECT extra FROM files WHERE path = 'a'").fetchone() == ("kept",)
    db.close()


def test_eight_threads_hash_without_losing_rows(tmp_path: Path) -> None:
    with Index(tmp_path / "index.sqlite3") as index:
        index.sync("g", [Entry(f"f{i}", i + 1, 1) for i in range(40)])

        def work() -> None:
            for row in index.pending(40):
                index.set_hash(row, "a" * 64)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert index.counts() == {"hashed": 40, "pending": 0}


def test_a_file_that_changes_while_read_is_not_recorded(tmp_path: Path) -> None:
    file = tmp_path / "a"
    file.write_bytes(b"one")
    with Index(":memory:") as index:
        index.sync("g", scan(tmp_path))
        file.write_bytes(b"changed")
        assert hash_pending(index, {"g": tmp_path}) == 0
        assert index.hashed() == []


def test_sha256_file_streams(tmp_path: Path) -> None:
    file = tmp_path / "big"
    file.write_bytes(b"z" * (3 * 1024 * 1024 + 7))
    assert sha256_file(file) == hashlib.sha256(b"z" * (3 * 1024 * 1024 + 7)).hexdigest()
