"""Where there is nothing to show, the page says what to do next."""

from __future__ import annotations

from pathlib import Path

from lantern_bridge.admin.i18n import FR
from test_admin import _admin, _client


def test_no_device_yet_says_how_to_pair_one(tmp_path: Path) -> None:
    page = _admin(_client(tmp_path, sources=True)).get("/admin/devices").text
    assert "No device is paired yet." in page and 'href="/admin/pairings"' in page and 'href="/admin/start"' in page


def test_no_invitation_says_how_to_use_one(tmp_path: Path) -> None:
    page = _admin(_client(tmp_path, sources=True)).get("/admin/invitations").text
    assert "No open invitation." in page and "give its code to the person" in page


def test_a_search_with_no_result_offers_to_clear_the_filters(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    (tmp_path / "games" / "Calm Tide [0100CCCC00000000][v0].nsp").write_bytes(b"fine")
    client.app.state.runtime.cycle(batch=10)  # type: ignore[attr-defined]
    page = client.get("/admin/library?q=zzzz").text
    assert "No title matches these filters." in page and 'href="/admin/library">Clear the filters' in page
    assert "Clear the filters" not in client.get("/admin/library?q=calm").text


def test_an_unknown_title_offers_a_search_for_its_id(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    gone = client.get("/admin/library/0100DEAD00000000")
    assert gone.status_code == 404 and "/admin/library?q=0100DEAD00000000" in gone.text
    odd = client.get("/admin/library/%3Cscript%3E")
    assert odd.status_code == 404 and "<script>" not in odd.text  # the id is escaped, never raw


def test_the_new_texts_have_their_french() -> None:
    for text in (
        "Open Lantern on the console and pick this server, then accept its request in",
        "Create one above and give its code to the person, who types it in Lantern when adding this server.",
        "Clear the filters",
        "It may have been renamed or removed from its folder.",
        "Search the library for",
    ):
        assert text in FR
