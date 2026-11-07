# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""A folder the settings open to changes: the pages may delete one of its files; nothing else may."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lantern.uid import new_uid

from lantern_bridge.app import make_app
from lantern_bridge.config import Config, FolderSource, load, save
from test_admin import _admin, _csrf

BASE = "Aurora Quest [0100AAAA00000000][v0].nsp"
UPDATE = "Aurora Quest [0100AAAA00000800][v65536].nsp"
OTHER = "Zebra Run [0100BBBB00000000][v0].nsp"


def _shelf(tmp_path: Path, *, writable: bool = True) -> tuple[TestClient, Path]:
    games = tmp_path / "games"
    games.mkdir(parents=True, exist_ok=True)
    for name, data in ((BASE, b"base"), (UPDATE, b"update!"), (OTHER, b"zebra")):
        (games / name).write_bytes(data)
    config = Config(
        uid=new_uid(),
        name="Test",
        atlas=False,
        update_check=False,
        sources=[FolderSource("games", games, writable=writable)],
    )
    path = tmp_path / "state" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    save(path, config)
    client = _admin(
        TestClient(
            make_app(config, tmp_path / "state", run=False, config_path=path),
            client=("127.0.0.1", 50000),
            follow_redirects=False,
        )
    )
    client.app.state.runtime.cycle(batch=50)  # type: ignore[attr-defined]
    return client, games


def _delete(client: TestClient, source: str, path: str, title: str = "0100AAAA00000000") -> object:
    token = _csrf(client.get(f"/admin/library/{title}").text)
    return client.post(f"/admin/library/{title}/delete", data={"csrf": token, "source": source, "path": path})


def test_a_folder_is_closed_to_changes_unless_the_settings_say_so(tmp_path: Path) -> None:
    folder = FolderSource("games", tmp_path)
    assert folder.writable is False
    config = Config(uid=new_uid(), name="T", sources=[folder])
    assert "writable" not in config.to_toml()  # a closed folder says nothing: the file is as it was
    open_ = Config(uid=config.uid, name="T", sources=[FolderSource("games", tmp_path, writable=True)])
    path = tmp_path / "config.toml"
    save(path, open_)
    assert "writable = true" in path.read_text() and load(path).sources[0].writable is True  # type: ignore[union-attr]
    path.write_text(path.read_text().replace("writable = true", 'writable = "yes"'))
    with pytest.raises(Exception, match="writable"):
        load(path)


def test_the_add_form_and_the_sources_page_open_and_close_a_folder(tmp_path: Path) -> None:
    client, games = _shelf(tmp_path, writable=False)
    sources = client.get("/admin/sources")
    assert "can change files" not in sources.text and "Allow changing files" in sources.text
    csrf = _csrf(sources.text)
    assert "Allow this bridge to delete and compress files in this folder" in sources.text
    reply = client.post("/admin/config/sources/games/writable", data={"csrf": csrf, "allow": "1"})
    assert reply.status_code == 303 and load(tmp_path / "state" / "config.toml").sources[0].writable is True  # type: ignore[union-attr]
    assert client.app.state.runtime.config.sources[0].writable is True  # type: ignore[attr-defined]  # applied at once
    assert "can change files" in client.get("/admin/sources").text
    client.post("/admin/config/sources/games/writable", data={"csrf": csrf, "allow": "0"})
    assert load(tmp_path / "state" / "config.toml").sources[0].writable is False  # type: ignore[union-attr]
    client.post(
        "/admin/config/sources",
        data={"csrf": csrf, "kind": "folder", "id": "more", "where": str(games), "writable": "on"},
    )
    assert load(tmp_path / "state" / "config.toml").sources[1].writable is True  # type: ignore[union-attr]
    assert client.post("/admin/config/sources/nowhere/writable", data={"csrf": csrf, "allow": "1"}).status_code == 400


def test_a_file_of_an_open_folder_can_be_deleted_and_only_that_file(tmp_path: Path) -> None:
    client, games = _shelf(tmp_path)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    page = client.get("/admin/library/0100AAAA00000000").text
    assert page.count('action="/admin/library/0100AAAA00000000/delete"') == 2  # one button a file
    reply = _delete(client, "games", UPDATE)
    assert reply.status_code == 303 and reply.headers["location"] == "/admin/library/0100AAAA00000000?m=deleted"  # type: ignore[attr-defined]
    assert not (games / UPDATE).exists() and (games / BASE).exists() and (games / OTHER).exists()
    assert "The file was deleted from the disk." in client.get(reply.headers["location"]).text  # type: ignore[attr-defined]
    events = [e for e in runtime.history.events() if e.kind == "deleted"]
    assert len(events) == 1 and events[0].subject == "0100AAAA00000000" and events[0].detail == "1 file"
    runtime.cycle(batch=50)  # the next pass forgets it
    assert UPDATE not in {f.name for f in runtime.browser.catalog().titles["0100AAAA00000000"].files}
    last = _delete(client, "games", BASE)  # the title's last file: back to the library
    assert last.headers["location"] == "/admin/library?m=deleted"  # type: ignore[attr-defined]


def test_nothing_is_deleted_in_a_closed_folder_or_outside_the_title(tmp_path: Path) -> None:
    client, games = _shelf(tmp_path, writable=False)
    assert "/delete" not in client.get("/admin/library/0100AAAA00000000").text  # no button
    refused = _delete(client, "games", UPDATE)
    assert refused.status_code == 403 and (games / UPDATE).exists()  # type: ignore[attr-defined]
    client2, games2 = _shelf(tmp_path / "second")
    assert _delete(client2, "games", OTHER).status_code == 403 and (games2 / OTHER).exists()  # type: ignore[attr-defined]  # not this title's
    assert _delete(client2, "games", "../outside.nsp").status_code == 403  # type: ignore[attr-defined]
    assert _delete(client2, "elsewhere", UPDATE).status_code == 403 and (games2 / UPDATE).exists()  # type: ignore[attr-defined]
    assert (
        client2.post("/admin/library/0100AAAA00000000/delete", data={"source": "games", "path": UPDATE}).status_code
        == 403
    )


def test_a_link_is_never_followed_out_of_the_folder(tmp_path: Path) -> None:
    client, games = _shelf(tmp_path)
    secret = tmp_path / "secret.nsp"
    secret.write_bytes(b"keep me")
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    (games / UPDATE).unlink()
    os.symlink(secret, games / UPDATE)  # the index never lists a link, but the request names one
    runtime.browser.catalog()  # the catalog still lists the old row: the request is checked against the disk
    assert _delete(client, "games", UPDATE).status_code == 403 and secret.exists()  # type: ignore[attr-defined]


def test_a_read_only_folder_says_so_instead_of_failing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, games = _shelf(tmp_path)

    def refuse(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError(30, "Read-only file system")

    monkeypatch.setattr(Path, "unlink", refuse)
    reply = _delete(client, "games", UPDATE)
    assert reply.status_code == 500 and "mounted read-only" in reply.text and (games / UPDATE).exists()  # type: ignore[attr-defined]


def test_the_texts_are_in_french_too(tmp_path: Path) -> None:
    client, _ = _shelf(tmp_path)
    client.post("/admin/lang", data={"lang": "fr", "next": "/admin/library"})
    page = client.get("/admin/library/0100AAAA00000000").text
    assert "Supprimer" in page and "irréversible" in page


def test_a_deletion_is_a_labelled_kind_of_the_journal(tmp_path: Path) -> None:
    client, _ = _shelf(tmp_path)
    _delete(client, "games", UPDATE)
    page = client.get("/admin/events").text
    assert "File deleted" in page and "kind=deleted" in page and "1 file" in page
    assert "File deleted" in client.get("/admin/events?kind=deleted").text
    assert "File converted" in page  # it has its place in the filter too
