# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""Hashing and converting on several cores: the count, the same result as one at a time, the settings."""

from __future__ import annotations

from pathlib import Path

import pytest
from lantern.server.file_index import SqliteIndex, hash_pending, sha256_file

from lantern_bridge.config import Config, ConfigError, parse_text
from lantern_bridge.hashing import AUTO_MAX, hash_many, scan_packages, sha_instructions, workers
from test_admin import _admin, _client, _csrf
from test_compress import _args, _convert, _ready, _wait, tool  # noqa: F401  (the fixture)
from test_writable import UPDATE, _shelf


def test_the_automatic_count_is_half_the_cores_between_one_and_the_ceiling() -> None:
    assert workers(0, cores=1) == 1 and workers(0, cores=2) == 1 and workers(0, cores=4) == 2
    assert workers(0, cores=64) == AUTO_MAX
    assert workers(7, cores=2) == 7  # what was asked is kept


def _index(tmp_path: Path, count: int = 12) -> tuple[SqliteIndex, dict[str, Path]]:
    root = tmp_path / "games"
    root.mkdir(parents=True)
    for n in range(count):
        (root / f"Game [0100AAAA0000{n:04x}] [v0].nsp").write_bytes(bytes([n]) * (1000 + n))
    index = SqliteIndex(tmp_path / "index.sqlite3")
    index.sync("games", scan_packages(root))
    return index, {"games": root}


def test_several_files_at_once_give_the_hashes_of_one_at_a_time(tmp_path: Path) -> None:
    index, roots = _index(tmp_path)
    seen: list[str] = []
    assert hash_many(index, roots, threads=4, limit=50, on_hashed=lambda s, p: seen.append(p)) == 12
    assert len(seen) == 12 and not index.pending(50)
    rows = {r.path: r.sha256 for r in index.hashed()}
    assert all(rows[p] == sha256_file(roots["games"] / p) for p in rows)
    again, other = _index(tmp_path / "b")
    assert hash_pending(again, other, limit=50) == 12
    assert {r.path: r.sha256 for r in again.hashed()} == rows


def test_a_file_that_changes_while_it_is_read_stays_pending(tmp_path: Path) -> None:
    index, roots = _index(tmp_path, 3)
    gone = next(iter(roots["games"].iterdir()))
    gone.unlink()
    assert hash_many(index, roots, threads=3, limit=50) == 2 and len(index.pending(50)) == 1


def test_the_settings_keep_the_threads_and_refuse_nonsense() -> None:
    config = parse_text(
        'uid = "6df832d0-7d7a-4b5e-9f2a-d8f775ed93f1"\nschema = 1\nhash_threads = 3\ncompress_threads = 8\n'
    )
    assert (config.hash_threads, config.compress_threads) == (3, 8)
    assert "hash_threads = 3" in config.to_toml()
    assert "hash_threads" not in Config(uid="u").to_toml()  # the default is not written
    for bad in ("-1", "65", "true", '"2"'):
        with pytest.raises(ConfigError):
            parse_text(f'uid = "6df832d0-7d7a-4b5e-9f2a-d8f775ed93f1"\nschema = 1\nhash_threads = {bad}\n')


def test_the_settings_page_saves_the_threads_and_the_diagnostic_says_them(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    csrf = _csrf(client.get("/admin/config").text)
    form = {
        "csrf": csrf,
        "name": "T",
        "remote_rate": "10",
        "admin_lan_only": "on",
        "hash_threads": "2",
        "compress_threads": "3",
    }
    assert client.post("/admin/config/settings", data=form).status_code == 303
    text = client.get("/admin/diagnostic").text
    assert "2 file(s) hashed at once" in text and "conversions on 3" in text
    assert "SHA-256 on" in text and sha_instructions() in text
    bad = client.post("/admin/config/settings", data={**form, "hash_threads": "many"})
    assert bad.status_code == 400


def test_nsz_is_given_the_threads_asked(tmp_path: Path, tool: Path) -> None:  # noqa: F811
    client, _ = _shelf(tmp_path)
    _ready(tmp_path)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    runtime.config.compress_threads = 3
    _convert(client, UPDATE, level="10", mode="block")
    _wait(runtime.compressor)
    args = _args(tmp_path)
    assert args[args.index("-t") + 1] == "3"


def test_the_export_does_not_say_why_the_file_could_not_be_read(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    (tmp_path / "state" / "config.toml").write_text("this is = = not toml")
    reply = client.get("/admin/config/export")
    assert reply.status_code == 500 and reply.text == "The settings file cannot be read."
