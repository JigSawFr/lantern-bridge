"""The library tab: titles grouped from the index, filters, a title's page."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from lantern_bridge.catalog import Catalog, search
from test_admin import _admin, _client, _csrf

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


def _post(client: TestClient, url: str, **data: str) -> object:
    token = client.get("/admin/library/0100AAAA00000000").text.split('name="csrf" value="')[1].split('"')[0]
    return client.post(url, data={"csrf": token, **data})


def test_one_file_can_be_hashed_again_first_in_line(tmp_path: Path) -> None:
    client = _shelf(tmp_path)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    before = {f.name: f.sha256 for f in _catalog(client).titles["0100AAAA00000000"].files}
    reply = _post(client, "/admin/library/0100AAAA00000000/rehash", source="games", path=UPDATE)
    assert reply.status_code == 200 and "Hashing again" in reply.text  # type: ignore[attr-defined]
    runtime.browser.forget()
    files = {f.name: f for f in _catalog(client).titles["0100AAAA00000000"].files}
    assert files[UPDATE].sha256 is None and files[BASE].sha256 == before[BASE]  # only that file was forgotten
    assert runtime.index.pending(1)[0].path == UPDATE
    runtime.cycle(batch=5)
    runtime.browser.forget()
    again = {f.name: f for f in _catalog(client).titles["0100AAAA00000000"].files}
    assert again[UPDATE].sha256 == before[UPDATE] and again[UPDATE].served  # the same bytes, the same hash
    assert ("games", UPDATE) in runtime.stats.timings
    assert " s" in client.get("/admin/library/0100AAAA00000000").text


def test_a_whole_title_can_be_hashed_again_and_only_its_own_files(tmp_path: Path) -> None:
    client = _shelf(tmp_path)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    assert _post(client, "/admin/library/0100AAAA00000000/rehash").status_code == 200  # type: ignore[attr-defined]
    assert runtime.index.counts() == {"hashed": 1, "pending": 3}  # the other title keeps its hash
    stranger = _post(client, "/admin/library/0100AAAA00000000/rehash", source="games", path=OTHER)
    assert stranger.status_code == 404  # type: ignore[attr-defined]
    assert _post(client, "/admin/library/0100FFFF00000000/rehash").status_code == 404  # type: ignore[attr-defined]


def test_the_command_line_hashes_one_title_again(tmp_path: Path, capsys: object) -> None:
    from lantern_bridge.__main__ import main

    client = _shelf(tmp_path)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    config = tmp_path / "state" / "config.toml"
    config.write_text(client.app.state.runtime.config.to_toml())  # type: ignore[attr-defined]
    runtime.index.close()
    assert main(["rehash", "--title", "0100bbbb00000000", "--config", str(config)]) == 0
    assert main(["rehash", "--title", "0100FFFF00000000", "--config", str(config)]) == 1


def test_the_covers_come_from_the_sheet_when_the_bridge_holds_them(tmp_path: Path) -> None:
    import dataclasses

    from lantern._generated.models import Image
    from lantern.server import FileRef

    client = _admin(_client(tmp_path, sources=True))
    (tmp_path / "games" / "Aurora Quest [0100AAAA00000000][v0].nsp").write_bytes(b"base")
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    runtime.cycle(batch=5)
    page = client.get("/admin/library/0100AAAA00000000")
    assert "image/icon" not in page.text
    assert client.get("/admin/library/0100AAAA00000000/image/icon").status_code == 404
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    ref = FileRef.of_bytes(png, "image/png")
    name = runtime.projection.library.add_media(ref, "png")
    sheet = runtime.projection.title("0100AAAA00000000")
    assert sheet is not None
    shown = dataclasses.replace(
        sheet,
        icon=Image(url=f"/api/lantern/v1/media/{name}", sha256=ref.sha256),
        screenshots=[Image(url=f"/api/lantern/v1/media/{name}", sha256=ref.sha256)],
    )
    runtime.projection.title = lambda _id: shown
    assert "/admin/library/0100AAAA00000000/image/icon" in client.get("/admin/library").text
    assert "image/shot-0" in client.get("/admin/library/0100AAAA00000000").text
    image = client.get("/admin/library/0100AAAA00000000/image/icon")
    assert image.status_code == 200 and image.content == png and image.headers["content-type"] == "image/png"
    assert client.get("/admin/library/0100AAAA00000000/image/shot-3").status_code == 404
    assert client.get("/admin/library/0100AAAA00000000/image/..%2Fx").status_code == 404


def test_a_package_the_name_does_not_identify_is_listed_as_ignored(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    games = tmp_path / "games"
    (games / "Aurora Quest [0100AAAA00000000][v0].nsp").write_bytes(b"base")
    (games / "mystery game.nsp").write_bytes(b"1234")
    (games / "notes.txt").write_bytes(b"not a package")
    client.app.state.runtime.cycle(batch=5)  # type: ignore[attr-defined]
    assert "1 files ignored" in client.get("/admin/library").text
    page = client.get("/admin/library/ignored")
    assert page.status_code == 200 and "mystery game.nsp" in page.text and "notes.txt" not in page.text
    (games / "mystery game.nsp").rename(games / "Mystery [0100BBBB00000000][v0].nsp")
    client.app.state.runtime.cycle(batch=5)  # type: ignore[attr-defined]
    assert "files ignored" not in client.get("/admin/library").text
    assert "Every package" in client.get("/admin/library/ignored").text
    assert _client(tmp_path).get("/admin/library/ignored").status_code == 303  # signed out: sent to the login


def test_duplicates_and_orphans_are_pointed_out(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    games = tmp_path / "games"
    (games / "Aurora Quest [0100AAAA00000000][v0].nsp").write_bytes(b"base")
    (games / "Aurora copy [0100AAAA00000000][v0].nsp").write_bytes(b"base")  # the same bytes
    (games / "Aurora Quest [0100AAAA00000800][v65536].nsp").write_bytes(b"one")
    (games / "Aurora Quest up [0100AAAA00000800][v65536].nsp").write_bytes(b"two")  # the same version, other bytes
    (games / "Night [0100BBBB00000800][v1].nsp").write_bytes(b"orphan")  # an update, no game
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    runtime.cycle(batch=20)
    kinds = [(i.kind, i.title_id) for i in runtime.browser.catalog().issues()]
    assert kinds == [
        ("same_content", "0100AAAA00000000"),
        ("same_version", "0100AAAA00000000"),
        ("no_base", "0100BBBB00000000"),
    ]
    page = client.get("/admin/library/checks")
    assert page.status_code == 200 and "The same file twice" in page.text and "without its game" in page.text
    assert "3 things to check" in client.get("/admin/library").text
    # Of the same bytes twice, the page says which file the library serves (the first path) and not the other.
    twice = next(i for i in runtime.browser.catalog().issues() if i.kind == "same_content")
    assert twice.served == "games: Aurora Quest [0100AAAA00000000][v0].nsp" and "Aurora copy" in twice.detail
    assert "Served:" in page.text and "games: Aurora Quest [0100AAAA00000000][v0].nsp" in page.text.split("Served:")[1]


def test_the_library_can_be_exported_as_json_and_as_csv(tmp_path: Path) -> None:
    import csv
    import io
    import json

    client = _admin(_client(tmp_path, sources=True))
    (tmp_path / "games" / "=SUM(1) [0100AAAA00000000][v0].nsp").write_bytes(b"base")
    client.app.state.runtime.cycle(batch=5)  # type: ignore[attr-defined]
    made = client.get("/admin/library/export")
    assert made.status_code == 200 and "attachment" in made.headers["content-disposition"]
    document = json.loads(made.text)
    assert document["schema"] == 1 and document["titles"][0]["title_id"] == "0100AAAA00000000"
    assert document["titles"][0]["files"][0]["sha256"] and "generated" in document
    rows = list(csv.reader(io.StringIO(client.get("/admin/library/export?format=csv").text)))
    assert rows[0][0] == "title_id" and rows[1][0] == "0100AAAA00000000"
    assert all(not cell.startswith("=") for row in rows for cell in row)  # a name cannot become a formula
    assert _client(tmp_path).get("/admin/library/export").status_code == 303


def test_columns_sort_both_ways_and_a_tie_goes_by_name(tmp_path: Path) -> None:
    catalog = _catalog(_shelf(tmp_path))

    def names(**kw: Any) -> list[str]:
        return [t.name for t in search(catalog, **kw).titles]

    assert names(sort="name") == ["Aurora Quest", "Zebra Run"] and names(sort="name", descending=True) == [
        "Zebra Run",
        "Aurora Quest",
    ]
    assert names(sort="size") == ["Aurora Quest", "Zebra Run"]  # the biggest first, unless told otherwise
    assert names(sort="size", descending=False) == ["Zebra Run", "Aurora Quest"]
    assert (
        names(sort="version") == ["Aurora Quest", "Zebra Run"]
        and names(sort="version", descending=False)[0] == "Zebra Run"
    )
    assert names(sort="nonsense") == names(sort="name")  # an unknown sort is the default


def test_the_list_has_sortable_headers_and_a_checkbox_a_title(tmp_path: Path) -> None:
    client = _shelf(tmp_path)
    page = client.get("/admin/library?view=list").text
    assert 'aria-sort="ascending"' in page and "sort=name&amp;dir=desc" in page  # the active column flips
    assert "sort=size&amp;dir=desc" in page and "sort=version&amp;dir=desc" in page  # the others start their way
    assert (
        page.count('data-pick value="0100') == 2
        and "data-pick-all" in page
        and 'action="/admin/library/rehash"' in page
    )
    by_size = client.get("/admin/library?view=list&sort=size&dir=asc").text
    assert 'aria-sort="ascending"' in by_size and by_size.index("Zebra Run") < by_size.index("Aurora Quest")
    assert "data-pick" not in client.get("/admin/library").text  # the grid has no selection


def test_titles_ticked_are_hashed_again_together(tmp_path: Path) -> None:
    client = _shelf(tmp_path)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    csrf = _csrf(client.get("/admin/library?view=list").text)
    reply = client.post(
        "/admin/library/rehash",
        data={
            "csrf": csrf,
            "ids": "0100aaaa00000000, 0100FFFF00000000",
            "next": "/admin/library?view=list&q=aur&m=old",
        },
    )
    assert reply.status_code == 303 and reply.headers["location"] == "/admin/library?view=list&q=aur&m=queued"
    assert runtime.index.counts() == {"hashed": 1, "pending": 3}  # the three files of one title, not the other's
    assert "Hashing again" in client.get(reply.headers["location"]).text
    nothing = client.post("/admin/library/rehash", data={"csrf": csrf, "ids": "", "next": "//evil.example.org"})
    assert nothing.headers["location"] == "/admin/library"  # nothing ticked, and an outside address is not followed
    assert client.post("/admin/library/rehash", data={"ids": "0100AAAA00000000"}).status_code == 403  # no token


def test_an_export_can_be_the_filtered_list_or_the_ticked_titles(tmp_path: Path) -> None:
    import csv
    import io
    import json

    client = _shelf(tmp_path)
    rows = list(csv.reader(io.StringIO(client.get("/admin/library/export?format=csv&q=zebra").text)))
    assert [r[0] for r in rows[1:]] == ["0100BBBB00000000"]
    ids = json.loads(client.get("/admin/library/export?ids=0100bbbb00000000,0100FFFF00000000").text)["titles"]
    assert [t["title_id"] for t in ids] == ["0100BBBB00000000"]  # an unknown id is skipped
    everything = json.loads(client.get("/admin/library/export").text)["titles"]
    assert len(everything) == 2
    assert len(json.loads(client.get("/admin/library/export?q=nothing-like-it").text)["titles"]) == 0
