"""Settings that are safer to change: the last change can be taken back, a source can be tried before it is saved."""

from __future__ import annotations

import os
import re
from pathlib import Path

from lantern_bridge.admin.i18n import FR
from lantern_bridge.config import load
from test_admin import _admin, _client, _csrf


def _settings(client, csrf: str, **fields: str):  # type: ignore[no-untyped-def]
    form = {"csrf": csrf, "name": "Shelf", "remote_rate": "10", "admin_lan_only": "on", **fields}
    return client.post("/admin/config/settings", data=form)


def test_the_last_change_can_be_taken_back_and_taken_again(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    path = tmp_path / "state" / "config.toml"
    csrf = _csrf(client.get("/admin/config").text)
    assert "Undo the last change" not in client.get("/admin/config").text  # nothing changed yet
    assert _settings(client, csrf, name="First").status_code == 303
    assert _settings(client, csrf, name="Second").status_code == 303
    assert load(path).name == "Second" and "Undo the last change" in client.get("/admin/config").text
    if os.name == "posix":  # Windows has no such mode bits
        assert (path.with_name("config.toml.previous").stat().st_mode & 0o077) == 0  # not readable by others
    undone = client.post("/admin/config/undo", data={"csrf": csrf})
    assert undone.status_code == 303 and undone.headers["location"].endswith("/config?m=undone")
    assert load(path).name == "First"
    client.post("/admin/config/undo", data={"csrf": csrf})  # again: the change comes back
    assert load(path).name == "Second"


def test_writing_the_same_settings_again_keeps_nothing_new(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    path = tmp_path / "state" / "config.toml"
    csrf = _csrf(client.get("/admin/config").text)
    _settings(client, csrf, name="First")
    _settings(client, csrf, name="Second")
    _settings(client, csrf, name="Second")  # the same again
    client.post("/admin/config/undo", data={"csrf": csrf})
    assert load(path).name == "First"  # the previous one is still the one before "Second"


def test_there_is_nothing_to_undo_at_first_and_a_visitor_cannot(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    csrf = _csrf(client.get("/admin/config").text)
    refused = client.post("/admin/config/undo", data={"csrf": csrf})
    assert refused.status_code == 400 and "There is nothing to undo." in refused.text
    assert client.post("/admin/config/undo", data={"csrf": "wrong"}).status_code in (400, 403)


def test_a_source_can_be_tried_without_saving_and_the_form_keeps_what_was_typed(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    path = tmp_path / "state" / "config.toml"
    csrf = _csrf(client.get("/admin/config").text)
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    folder = tmp_path / "more"
    folder.mkdir()
    form = {"csrf": csrf, "kind": "folder", "id": "more", "where": str(folder)}
    tried = client.post("/admin/config/sources/probe", data=form)
    assert tried.status_code == 200 and "The folder is there and can be read." in tried.text
    assert 'value="more"' in tried.text and str(folder) in tried.text  # nothing to type again
    assert (path.read_text(encoding="utf-8") if path.exists() else "") == before  # nothing saved
    missing = client.post("/admin/config/sources/probe", data={**form, "where": str(tmp_path / "nowhere")})
    assert "The folder cannot be read." in missing.text


def test_a_refused_source_comes_back_with_its_fields_and_its_reason(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    csrf = _csrf(client.get("/admin/config").text)
    bad = client.post(
        "/admin/config/sources/probe",
        data={"csrf": csrf, "kind": "tinfoil", "id": "shop", "where": "http://x/", "password_env": "no spaces"},
    )
    assert bad.status_code == 400 and 'value="shop"' in bad.text and "a variable" in bad.text
    assert re.search(r"<option value=\"tinfoil\" selected>", bad.text)
    nope = client.post("/admin/config/sources/probe", data={"csrf": csrf, "kind": "ftp", "id": "x", "where": "y"})
    assert nope.status_code == 400
    twice = client.post(
        "/admin/config/sources", data={"csrf": csrf, "kind": "folder", "id": "games", "where": str(tmp_path / "games")}
    )
    assert twice.status_code == 400 and 'value="games"' in twice.text  # a duplicate name keeps the form filled


def test_every_new_text_has_its_french() -> None:
    for text in (
        "Try it first",
        "Try it first reads the source without saving anything.",
        "Undo the last change",
        "There is nothing to undo.",
        "New source",
        "this kind of source is not supported",
    ):
        assert text in FR
