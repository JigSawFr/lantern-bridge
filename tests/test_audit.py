# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The journal says who changed the settings, from where, and what (never a value that may be private)."""

from __future__ import annotations

from pathlib import Path

from lantern_bridge.config import Config, FolderSource, changes
from test_admin import _admin, _client, _csrf


def test_changes_name_the_settings_and_show_a_switch_but_never_a_text() -> None:
    before = Config(uid="u", name="Home", public=False)
    after = Config(uid="u", name="Secret name", public=True, remote_rate=4.0)
    lines = changes(before, after)
    assert "public: false -> true" in lines and "remote_rate: 10.0 -> 4.0" in lines
    assert "name" in lines and not any("Secret" in line or "Home" in line for line in lines)
    assert changes(before, before) == []


def test_a_source_that_comes_or_goes_is_named_without_its_fields() -> None:
    one = Config(uid="u", sources=[FolderSource(id="games", path=Path("/srv/games"))])
    two = Config(uid="u")
    assert changes(one, two) == ["sources.games removed"]
    assert changes(two, one) == ["sources.games added"]


def test_the_journal_line_has_the_account_the_address_and_the_change(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    csrf = _csrf(client.get("/admin/config").text)
    form = {"csrf": csrf, "remote_rate": "10", "admin_lan_only": "on", "name": "Audited"}
    assert client.post("/admin/config/settings", data=form).status_code in (200, 303)
    page = client.get("/admin/events?kind=settings").text
    assert "boss" in page and "(owner)" in page
    assert "name" in page and "Audited" not in page.split("<h1>")[1].split("</nav>")[1]  # the value stays out
