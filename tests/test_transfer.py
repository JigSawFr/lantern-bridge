# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The settings leave as one file and come back through a page that says what would change first."""

from __future__ import annotations

import re
from pathlib import Path

from lantern_bridge.config import load
from test_admin import _admin, _client, _csrf


def _setup(tmp_path: Path):  # type: ignore[no-untyped-def]
    client = _admin(_client(tmp_path, sources=True))
    return client, _csrf(client.get("/admin/config").text)


def test_the_export_is_the_settings_file_and_holds_no_secret(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    reply = client.get("/admin/config/export")
    assert reply.status_code == 200 and "attachment" in reply.headers["content-disposition"]
    assert 'name = "' in reply.text and "schema" in reply.text


def test_an_import_shows_the_changes_first_and_applies_on_the_second_step(tmp_path: Path) -> None:
    client, csrf = _setup(tmp_path)
    text = client.get("/admin/config/export").text.replace("public = false", "public = true")
    first = client.post("/admin/config/import", data={"csrf": csrf, "toml": text})
    assert first.status_code == 200 and "public: false -&gt; true" in first.text
    assert not load(tmp_path / "state" / "config.toml").public  # nothing is written by the preview
    second = client.post("/admin/config/import", data={"csrf": csrf, "toml": text, "confirm": "1"})
    assert second.status_code == 303
    assert load(tmp_path / "state" / "config.toml").public
    journal = client.get("/admin/events?kind=settings").text
    assert "imported" in journal and "public: false -&gt; true" in journal


def test_an_import_keeps_the_identity_of_the_bridge(tmp_path: Path) -> None:
    client, csrf = _setup(tmp_path)
    exported = client.get("/admin/config/export").text
    uid = re.search(r'uid = "([^"]*)"', exported).group(1)  # type: ignore[union-attr]
    text = re.sub(r'uid = "[^"]*"', 'uid = "someone-else"', exported)
    text = text.replace("public = false", "public = true")
    client.post("/admin/config/import", data={"csrf": csrf, "toml": text, "confirm": "1"})
    assert load(tmp_path / "state" / "config.toml").uid == uid


def test_a_bad_file_or_one_that_changes_nothing_is_refused_with_a_sentence(tmp_path: Path) -> None:
    client, csrf = _setup(tmp_path)
    bad = client.post("/admin/config/import", data={"csrf": csrf, "toml": "this is = = not toml"})
    assert bad.status_code == 400 and "The file cannot be used" in bad.text
    same = client.post("/admin/config/import", data={"csrf": csrf, "toml": client.get("/admin/config/export").text})
    assert same.status_code == 200 and "This file changes nothing." in same.text
    newer = client.post("/admin/config/import", data={"csrf": csrf, "toml": 'schema = 999\nuid = "x"\n'})
    assert newer.status_code == 400
