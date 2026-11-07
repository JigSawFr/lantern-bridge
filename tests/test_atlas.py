"""Atlas: the copy of its games file, its refresh, and the sheets it fills in. No test touches the network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from lantern_bridge.atlas import MAX_BYTES, Atlas, parse
from lantern_bridge.config import Config, load, save
from test_admin import _admin, _client

QUEST = "0100AAAA00000000"
DOC = {
    "schema": 1,
    "platform": "switch",
    "games": {
        QUEST: {
            "name": "Aurora Quest™",
            "series": [["Q1", "Aurora"]],
            "developers": [["Q2", "Boreal Works"]],
            "publishers": [["Q3", "Northern Lights"]],
            "genres": [["Q4", "platformer"], ["Q5", "3D"]],
            "modes": [["Q6", "single-player"]],
            "steam": {"app": 7, "positive": 90, "negative": 10},
            "unknown_future_key": 1,
        },
        "0100BBBB00000000": {"name": "Sparse"},
        "nonsense": {"name": "x"},
        "0100CCCC00000000": {"name": ""},
    },
}


def _transport(
    document: object = DOC, *, status: int = 200, etag: str = '"v1"', seen: list[httpx.Request] | None = None
) -> httpx.MockTransport:
    def answer(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.headers.get("if-none-match") == etag and status == 200:
            return httpx.Response(304)
        body = document if isinstance(document, bytes) else json.dumps(document).encode()
        return httpx.Response(status, content=body, headers={"etag": etag})

    return httpx.MockTransport(answer)


def _atlas(tmp_path: Path, transport: httpx.MockTransport) -> Atlas:
    return Atlas(
        tmp_path / "atlas.json", "https://atlas.example.org/games.json", client=httpx.Client(transport=transport)
    )


def test_the_games_file_is_read_defensively() -> None:
    games = parse(DOC)
    assert games is not None and sorted(games) == [QUEST, "0100BBBB00000000"]  # a bad id or an empty name is skipped
    quest = games[QUEST]
    assert quest.name == "Aurora Quest™" and quest.genres == ("platformer", "3D") and quest.rating == 90
    assert games["0100BBBB00000000"].genres == () and games["0100BBBB00000000"].rating is None
    assert parse({**DOC, "schema": 2}) is None  # a newer schema is treated as absent
    assert parse({**DOC, "schema": True}) is None and parse([]) is None and parse({"schema": 1, "games": []}) is None


def test_refresh_keeps_a_copy_and_asks_again_politely(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    atlas = _atlas(tmp_path, _transport(seen=seen))
    assert atlas.due() and atlas.refresh() and len(atlas) == 2 and atlas.error is None
    entry = atlas.get(QUEST.lower())
    assert entry is not None and entry.name == "Aurora Quest™"
    assert not atlas.refresh()  # 304: nothing changed
    assert seen[1].headers["if-none-match"] == '"v1"' and not atlas.due()
    again = _atlas(tmp_path, _transport(status=500))  # a new process starts from the copy on disk
    assert len(again) == 2 and again.get(QUEST) is not None and again.fetched is not None


def test_a_failed_fetch_keeps_the_copy_and_says_why(tmp_path: Path) -> None:
    atlas = _atlas(tmp_path, _transport())
    atlas.refresh()
    atlas._client = httpx.Client(transport=_transport(status=503, etag="x"))
    assert not atlas.refresh() and "503" in (atlas.error or "") and len(atlas) == 2
    for bad in (b"not json", json.dumps({**DOC, "schema": 9}).encode(), b"x" * (MAX_BYTES + 1)):
        atlas._client = httpx.Client(transport=_transport(bad, etag="y"))
        assert not atlas.refresh() and atlas.error and len(atlas) == 2


def test_a_copy_of_another_version_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "atlas.json").write_text(json.dumps({"schema": 99, "atlas": DOC}))
    assert len(_atlas(tmp_path, _transport(status=500))) == 0
    (tmp_path / "atlas.json").write_text("garbage")
    assert len(_atlas(tmp_path, _transport(status=500))) == 0


def test_the_setting_is_on_by_default_and_kept_in_the_file(tmp_path: Path) -> None:
    assert Config(uid="0c9c2a8e-5d3a-4c7e-8b0a-6f1d2e3c4b5a").atlas is True
    path = tmp_path / "config.toml"
    save(path, Config(uid="0c9c2a8e-5d3a-4c7e-8b0a-6f1d2e3c4b5a", atlas=False))
    assert "atlas = false" in path.read_text() and load(path).atlas is False


def _shelf(tmp_path: Path) -> TestClient:
    client = _admin(_client(tmp_path, sources=True))
    (tmp_path / "games" / f"Aurora Quest [{QUEST}][v0].nsp").write_bytes(b"base")
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    runtime.atlas = runtime.projection.atlas = _atlas(tmp_path, _transport())
    runtime.atlas.refresh()
    runtime.cycle(batch=5)
    return client


def test_atlas_fills_the_sheet_of_a_title_the_sources_say_nothing_of(tmp_path: Path) -> None:
    client = _shelf(tmp_path)
    sheet = client.app.state.runtime.projection.title(QUEST)  # type: ignore[attr-defined]
    assert sheet is not None and sheet.name == "Aurora Quest™"
    assert (
        sheet.developer == "Boreal Works"
        and sheet.publisher == "Northern Lights"
        and sheet.genres == ["platformer"]
        and sheet.tags == ["platformer", "3D"]
    )
    page = client.get(f"/admin/library/{QUEST}")
    assert "From Atlas" in page.text and "Aurora" in page.text and "90 %" in page.text and "single-player" in page.text


def test_with_the_setting_off_nothing_is_added(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    (tmp_path / "games" / f"Aurora Quest [{QUEST}][v0].nsp").write_bytes(b"base")
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    runtime.atlas = runtime.projection.atlas = None
    runtime.cycle(batch=5)
    sheet = runtime.projection.title(QUEST)
    assert sheet is not None and sheet.genres is None and sheet.developer is None
    assert "From Atlas" not in client.get(f"/admin/library/{QUEST}").text


def test_wikidatas_genre_labels_become_lanterns_genres() -> None:
    from lantern_bridge.atlas import genres_of

    assert genres_of(("collect-a-thon platformer", "3D platform game")) == ["platformer"]
    assert genres_of(("action-adventure game", "post-apocalyptic video game")) == ["adventure", "action"]
    assert genres_of(("farming simulator", "party video game")) == ["simulation", "party"]
    assert genres_of(("something nobody mapped",)) == []
