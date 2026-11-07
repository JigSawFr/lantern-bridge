"""The library tab: titles grouped from the index, filters, a title's page."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from lantern_bridge.catalog import Catalog, search
from test_admin import _admin, _client

BASE = "Aurora Quest [0100AAAA00000000][v0].nsp"
UPDATE = "Aurora Quest [0100AAAA00000800][v65536].nsp"
DLC = "Aurora Quest Moon Pack [0100AAAA00001001][v0].nsp"
OTHER = "Zebra Run [0100BBBB00000000][v0].nsp"


def _shelf(tmp_path: Path, *, cycle: bool = True) -> TestClient:
    client = _admin(_client(tmp_path, sources=True))
    games = tmp_path / "games"
    for name, data in ((BASE, b"base"), (UPDATE, b"update!"), (DLC, b"dlc"), (OTHER, b"zebra"), ("notes.nsp", b"?")):
        (games / name).write_bytes(data)
    if cycle:
        client.app.state.runtime.cycle(batch=50)  # type: ignore[attr-defined]
    return client


def _catalog(client: TestClient) -> Catalog:
    return client.app.state.runtime.browser.catalog()  # type: ignore[attr-defined,no-any-return]


def test_the_library_groups_files_by_title(tmp_path: Path) -> None:
    client = _shelf(tmp_path)
    catalog = _catalog(client)
    assert sorted(catalog.titles) == ["0100AAAA00000000", "0100BBBB00000000"]
    quest = catalog.titles["0100AAAA00000000"]
    assert quest.types == {"base", "update", "dlc"} and quest.latest == 65536 and quest.pending == 0
    assert [f.type for f in quest.files] == ["base", "update", "dlc"] and all(f.served for f in quest.files)
    assert catalog.counts() == {"all": 2, "base": 2, "update": 1, "dlc": 1}


def test_a_file_not_hashed_yet_shows_as_waiting(tmp_path: Path) -> None:
    client = _shelf(tmp_path, cycle=False)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    runtime.index.sync("games", __import__("lantern_bridge.hashing", fromlist=["x"]).scan_packages(tmp_path / "games"))
    quest = _catalog(client).titles["0100AAAA00000000"]
    assert quest.pending == 3 and not any(f.served for f in quest.files)
    page = client.get("/admin/library?state=pending")
    assert page.status_code == 200 and "Aurora Quest" in page.text and "waiting" in page.text


def test_filters_search_and_sort(tmp_path: Path) -> None:
    catalog = _catalog(_shelf(tmp_path))
    assert [t.name for t in search(catalog).titles] == ["Aurora Quest", "Zebra Run"]
    assert [t.name for t in search(catalog, q="zebra").titles] == ["Zebra Run"]
    assert [t.name for t in search(catalog, q="0100aaaa").titles] == ["Aurora Quest"]
    assert [t.name for t in search(catalog, kind="dlc").titles] == ["Aurora Quest"]
    assert search(catalog, source="nowhere").total == 0
    assert search(catalog, kind="nonsense").total == 2  # an unknown value is no filter
    assert next(t.name for t in search(catalog, sort="size").titles) == "Aurora Quest"
    assert search(catalog, per_page=1, page=9).page == 2  # out of range lands on the last page


def test_the_library_page_and_a_title_page(tmp_path: Path) -> None:
    client = _shelf(tmp_path)
    shelf = client.get("/admin/library")
    assert shelf.status_code == 200 and "Aurora Quest" in shelf.text and "Zebra Run" in shelf.text
    assert "style=" not in shelf.text and "tone-" in shelf.text  # the CSP forbids inline styles
    assert "Zebra Run" not in client.get("/admin/library?q=aurora").text
    page = client.get("/admin/library/0100aaaa00000000")
    assert page.status_code == 200 and "0100AAAA00000000" in page.text
    for name in (BASE, UPDATE, DLC):
        assert name in page.text
    assert client.get("/admin/library/0100FFFF00000000").status_code == 404
    assert client.get("/admin/library?page=abc&sort=zzz&type=%00").status_code == 200


def test_the_library_needs_the_administrator(tmp_path: Path) -> None:
    client = _client(tmp_path, sources=True)
    assert client.get("/admin/library").status_code == 303


def test_the_library_page_is_french_too(tmp_path: Path) -> None:
    client = _shelf(tmp_path)
    client.post("/admin/lang", data={"lang": "fr", "next": "/admin/library"})
    page = client.get("/admin/library")
    assert "Bibliothèque" in page.text and "Mises à jour" in page.text
