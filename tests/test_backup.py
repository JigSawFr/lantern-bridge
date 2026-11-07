"""The backup: what it holds, what it refuses, and that a restoration puts things back."""

from __future__ import annotations

import contextlib
import io
import json
import re
import sqlite3
import zipfile
from pathlib import Path

import pytest

from lantern_bridge import backup
from lantern_bridge.__main__ import main
from lantern_bridge.config import Config, FolderSource, load, save
from test_admin import _admin, _client

UID = "0c9c2a8e-5d3a-4c7e-8b0a-6f1d2e3c4b5a"


def _state(tmp_path: Path, rows: int = 3) -> tuple[Path, Path]:
    state = tmp_path / "state"
    state.mkdir()
    config = state / "config.toml"
    save(config, Config(uid=UID, name="Shelf", sources=[FolderSource("games", tmp_path)]))
    for name in backup.DATABASES:
        with contextlib.closing(sqlite3.connect(state / name)) as db, db:
            db.execute("create table t (n integer)")
            db.executemany("insert into t values (?)", [(i,) for i in range(rows)])
    return state, config


def _rows(path: Path) -> int:
    with contextlib.closing(sqlite3.connect(path)) as db:
        return int(db.execute("select count(*) from t").fetchone()[0])


def test_the_archive_holds_the_settings_and_the_databases_and_no_accounts(tmp_path: Path) -> None:
    state, config = _state(tmp_path)
    (state / "accounts.sqlite3").write_bytes(b"tokens")
    out = tmp_path / "b.zip"
    assert backup.make(state, config, out, now=1_000_000) == ["manifest.json", "config.toml", *backup.DATABASES]
    with zipfile.ZipFile(out) as archive:
        assert set(archive.namelist()) == backup.NAMES
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["schema"] == 1 and manifest["created"] == 1_000_000 and manifest["bridge"]["uid"] == UID


def test_a_restoration_puts_back_what_was_saved_and_keeps_what_it_replaced(tmp_path: Path) -> None:
    state, config = _state(tmp_path, rows=3)
    out = tmp_path / "b.zip"
    backup.make(state, config, out)
    save(config, Config(uid=UID, name="Changed"))
    with contextlib.closing(sqlite3.connect(state / "index.sqlite3")) as db, db:
        db.execute("insert into t values (99)")
    (state / "index.sqlite3-wal").write_bytes(b"old journal")
    assert backup.restore(out, state, config) == ["config.toml", *backup.DATABASES]
    assert load(config).name == "Shelf" and _rows(state / "index.sqlite3") == 3
    assert _rows(state / "index.sqlite3.before-restore") == 4 and not (state / "index.sqlite3-wal").exists()
    assert "Changed" in (state / "config.toml.before-restore").read_text()


def test_a_restoration_on_a_new_machine_needs_nothing_to_replace(tmp_path: Path) -> None:
    state, config = _state(tmp_path)
    out = tmp_path / "b.zip"
    backup.make(state, config, out)
    fresh = tmp_path / "new" / "state"
    assert backup.restore(out, fresh, fresh / "config.toml") == ["config.toml", *backup.DATABASES]
    assert load(fresh / "config.toml").uid == UID


def _archive(tmp_path: Path, files: dict[str, bytes]) -> Path:
    path = tmp_path / "x.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


GOOD_CONFIG = f'schema = 1\nuid = "{UID}"\nname = "Shelf"\n'.encode()


@pytest.mark.parametrize(
    ("files", "why"),
    [
        ({}, "no manifest"),
        ({"manifest.json": b"{}", "config.toml": GOOD_CONFIG}, "no schema"),
        ({"manifest.json": b"nope", "config.toml": GOOD_CONFIG}, "not readable"),
        ({"manifest.json": b'{"schema": 2}', "config.toml": GOOD_CONFIG}, "schema 2"),
        ({"manifest.json": b'{"schema": true}', "config.toml": GOOD_CONFIG}, "no schema"),
        ({"manifest.json": b'{"schema": 1}', "config.toml": GOOD_CONFIG, "../evil": b"x"}, "does not restore"),
        ({"manifest.json": b'{"schema": 1}', "config.toml": b"schema = 99"}, "not valid"),
    ],
)
def test_an_archive_that_cannot_be_used_is_refused_and_nothing_is_touched(
    tmp_path: Path, files: dict[str, bytes], why: str
) -> None:
    state, config = _state(tmp_path)
    before = config.read_text()
    with pytest.raises(backup.BackupError, match=why):
        backup.restore(_archive(tmp_path, files), state, config)
    assert config.read_text() == before and not list(state.glob("*.before-restore"))


def test_a_file_that_is_no_zip_is_refused(tmp_path: Path) -> None:
    state, config = _state(tmp_path)
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip")
    with pytest.raises(backup.BackupError, match="not a backup"):
        backup.restore(junk, state, config)


def test_the_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, config = _state(tmp_path)
    out = tmp_path / "cli.zip"
    assert main(["backup", "--config", str(config), "-o", str(out)]) == 0 and out.exists()
    assert "index.sqlite3" in capsys.readouterr().out
    assert main(["restore", "--config", str(config), str(out)]) == 0
    assert "restored" in capsys.readouterr().out
    assert main(["restore", "--config", str(config), str(tmp_path / "missing.zip")]) == 1
    assert "not a backup" in capsys.readouterr().err


def test_the_settings_page_gives_the_archive(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    config = tmp_path / "state" / "config.toml"
    save(config, runtime.config)
    page = client.get("/admin/config")
    assert "Download a backup" in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
    assert client.post("/admin/config/backup", data={}).status_code == 403
    reply = client.post("/admin/config/backup", data={"csrf": csrf})
    assert reply.status_code == 200 and reply.headers["content-type"] == "application/zip"
    assert "lantern-bridge-backup-" in reply.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(reply.content)) as archive:
        assert {"manifest.json", "config.toml", "index.sqlite3"} <= set(archive.namelist())
    config.unlink()  # no settings file to save: the page says so rather than failing
    assert client.post("/admin/config/backup", data={"csrf": csrf}).status_code == 400
