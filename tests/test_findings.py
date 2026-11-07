"""The library points at its findings: a badge, a filter, the title's own list, the page by kind."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from lantern_bridge.admin.i18n import FR
from lantern_bridge.catalog import search
from test_admin import _admin, _client
from test_observer import _with_observer


def _seeded(tmp_path: Path) -> TestClient:
    client = _admin(_client(tmp_path, sources=True))
    games = tmp_path / "games"
    (games / "Aurora Quest [0100AAAA00000000][v0].nsp").write_bytes(b"base")
    (games / "Aurora copy [0100AAAA00000000][v0].nsp").write_bytes(b"base")  # the same bytes
    (games / "Aurora Quest [0100AAAA00000800][v65536].nsp").write_bytes(b"one")
    (games / "Aurora Quest up [0100AAAA00000800][v65536].nsp").write_bytes(b"two")  # the same version
    (games / "Night [0100BBBB00000800][v1].nsp").write_bytes(b"orphan")  # an update, no game
    (games / "Calm Tide [0100CCCC00000000][v0].nsp").write_bytes(b"fine")  # nothing to say
    client.app.state.runtime.cycle(batch=30)  # type: ignore[attr-defined]
    return client


def test_a_title_with_a_finding_has_a_badge_and_a_filter(tmp_path: Path) -> None:
    client = _seeded(tmp_path)
    page = client.get("/admin/library").text
    assert page.count('<span class="badge wait">to check</span>') == 2  # Aurora Quest, Night
    only = client.get("/admin/library?state=check").text
    assert "Aurora Quest" in only and "Night" in only and "Calm Tide" not in only
    assert "Calm Tide" in client.get("/admin/library?state=hashed").text  # the other filters are as before


def test_search_takes_the_flagged_titles_and_ignores_the_filter_without_them(tmp_path: Path) -> None:
    client = _seeded(tmp_path)
    catalog = client.app.state.runtime.browser.catalog()  # type: ignore[attr-defined]
    flagged = catalog.flagged()
    assert set(flagged) == {"0100AAAA00000000", "0100BBBB00000000"}
    assert [i.kind for i in flagged["0100AAAA00000000"]] == ["same_content", "same_version"]
    found = search(catalog, state="check", flagged=flagged)
    assert {t.title_id for t in found.titles} == set(flagged)
    assert search(catalog, state="check").total == 0  # no finding given, nothing flagged


def test_the_title_lists_its_own_findings(tmp_path: Path) -> None:
    client = _seeded(tmp_path)
    page = client.get("/admin/library/0100AAAA00000000").text
    assert "Things to check" in page and "The same file twice" in page and "Served:" in page
    assert "Things to check" not in client.get("/admin/library/0100CCCC00000000").text


def test_the_page_of_findings_can_be_cut_by_kind(tmp_path: Path) -> None:
    client = _seeded(tmp_path)
    everything = client.get("/admin/library/checks").text
    assert "The same file twice" in everything and "without its game" in everything
    twice = client.get("/admin/library/checks?kind=same_content").text
    assert "Aurora" in twice and "Night" not in twice
    orphans = client.get("/admin/library/checks?kind=no_base").text
    assert "Night" in orphans and "Aurora" not in orphans.split("<tbody>")[1]
    assert "Night" in client.get("/admin/library/checks?kind=bogus").text  # an unknown kind is no filter


def test_the_observer_reads_them_too(tmp_path: Path) -> None:
    boss, eye = _with_observer(tmp_path)
    games = tmp_path / "games"
    (games / "Aurora Quest [0100AAAA00000000][v0].nsp").write_bytes(b"base")
    (games / "Aurora copy [0100AAAA00000000][v0].nsp").write_bytes(b"base")
    boss.app.state.runtime.cycle(batch=30)  # type: ignore[attr-defined]
    assert "The same file twice" in eye.get("/admin/library/checks?kind=same_content").text
    assert eye.get("/admin/library?state=check").status_code == 200


def test_the_new_texts_have_their_french() -> None:
    for text in (
        "With something to check",
        "to check",
        "Finding",
        "Nothing is changed here: the files stay where they are.",
    ):
        assert text in FR
