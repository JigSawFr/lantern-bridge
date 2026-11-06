from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from lantern.uid import new_uid

from lantern_bridge.app import make_app
from lantern_bridge.config import Config, OwnfoilSource, load, save
from lantern_bridge.runtime import CHANGED_EVERY, Runtime
from lantern_bridge.sources import ownfoil
from lantern_bridge.sources.ownfoil import Ownfoil, Picture, _finished, file_name, image_kind
from lantern_bridge.sources.tinfoil import UpstreamError

BASE = "http://ownfoil.test:8465/"
PAYLOAD = b"frost" * 2000
ICON = b"\x89PNG\r\n\x1a\n" + b"icon" * 50
BANNER = b"\xff\xd8\xff\xe0" + b"banner" * 50
GAME = "0100ABCD00010000"


def _app(
    app_id: str = GAME, version: int = 0, token: str = "tok1", name: str | None = "Frost Garden", **kw: Any
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "appId": app_id,
        "appVersion": version,
        "downloadUrl": f"/api/download/{token}",
        "downloadSize": len(PAYLOAD),
        "downloadExtension": "nsp",
        "title": {"name": name},
    }
    return item | kw


def _title(**kw: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "titleId": GAME,
        "name": "Frost Garden",
        "intro": "A garden, frozen.",
        "description": "Grow plants in the cold.",
        "developer": "Cold Studio",
        "publisher": "Ice Press",
        "releaseDate": "2021-03-04",
        "category": ["Puzzle", "Arcade"],
        "languages": ["en", "fr"],
        "icon": {"url": "/api/media/x/icon/0/client/a.png", "local": True, "width": 256, "height": 256},
        "banner": {"url": "/api/media/x/banner/0/client/b.jpg", "local": True, "width": 720, "height": 405},
        "screenshots": [{"url": "https://cdn.example.org/s.png", "local": False}],
    }
    return item | kw


def _fake(
    apps: list[dict[str, Any]], titles: list[dict[str, Any]] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    """A fake Ownfoil: the GraphQL API (paged), the media and the downloads."""

    def page(items: list[dict[str, Any]], variables: dict[str, int]) -> dict[str, Any]:
        first = (variables["page"] - 1) * variables["size"]
        return {"total": len(items), "items": items[first : first + variables["size"]]}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/graphql":
            body = json.loads(request.content)
            data = (
                {"apps": page(apps, body["variables"])}
                if "apps(" in body["query"]
                else {"titles": page(titles or [], body["variables"])}
            )
            return httpx.Response(200, json={"data": data})
        if path.startswith("/api/media/"):
            return httpx.Response(200, content=ICON if path.endswith(".png") else BANNER)
        if path.startswith("/api/download/"):
            wanted = request.headers.get("range")
            if wanted is None:
                return httpx.Response(200, content=PAYLOAD)
            first, _, last = wanted.removeprefix("bytes=").partition("-")
            return httpx.Response(206, content=PAYLOAD[int(first) : int(last) + 1 if last else None])
        return httpx.Response(404)

    return handler


def _ownfoil(handler: Callable[[httpx.Request], httpx.Response], **kw: str) -> Ownfoil:
    return Ownfoil(BASE, client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def test_files_are_listed_from_the_api_page_after_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ownfoil, "APPS_PAGE", 2)
    apps = [
        _app(token="t1"),
        _app("0100ABCD00010800", 65536, "t2"),
        _app("0100ABCD00011000", 0, "t3", name="Frost DLC"),
    ]
    listed = list(_ownfoil(_fake(apps)).listing())
    assert [item.name for item in listed] == [
        "Frost Garden [0100ABCD00010000][v0].nsp",
        "Frost Garden [0100ABCD00010800][v65536].nsp",
        "Frost DLC [0100ABCD00011000][v0].nsp",
    ]
    assert listed[1].url == "http://ownfoil.test:8465/api/download/t2"
    assert {item.size for item in listed} == {len(PAYLOAD)}


def test_scan_gives_index_keys_the_relay_can_read_back() -> None:
    shop = _ownfoil(_fake([_app()]))
    (entry,) = list(shop.scan())
    assert entry.path == "api/download/tok1#Frost Garden [0100ABCD00010000][v0].nsp"
    assert shop.address(entry.path) == "http://ownfoil.test:8465/api/download/tok1"
    assert b"".join(shop.stream(entry.path, 5, 10)) == PAYLOAD[5:15]


def test_an_app_the_shop_describes_badly_is_left_out() -> None:
    apps = [
        _app(token="ok"),
        _app(token="far", downloadUrl="http://elsewhere.test/api/download/x"),
        _app(token="ext", downloadExtension="exe"),
        _app(token="size", downloadSize=-1),
        _app(token="bool", downloadSize=True),
        _app(token="id", appId="not an id"),
        _app(token="ver", appVersion="3"),
        _app(token="cred", downloadUrl="http://user:pw@ownfoil.test:8465/api/download/x"),
        _app(token="none", downloadUrl=None),
    ]
    assert [e.path.split("#")[0] for e in _ownfoil(_fake(apps)).scan()] == ["api/download/ok"]


def test_a_name_cannot_carry_markup_into_the_file_name() -> None:
    assert file_name("A/B [x]#\n", GAME, 1, "nsp") == f"AB x [{GAME}][v1].nsp"
    assert file_name(None, GAME, 0, "xci") == f"{GAME} [{GAME}][v0].xci"


def test_a_graphql_error_is_an_upstream_error_that_repeats_nothing() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "SECRET path /srv/games"}], "data": None})

    with pytest.raises(UpstreamError) as caught:
        list(_ownfoil(handler).listing())
    assert "SECRET" not in str(caught.value)
    for body in (b"not json", b"[]", b'{"data": 3}'):
        with pytest.raises(UpstreamError):
            list(_ownfoil(lambda _, body=body: httpx.Response(200, content=body)).listing())  # type: ignore[misc]
    with pytest.raises(UpstreamError):
        list(_ownfoil(lambda _: httpx.Response(401)).listing())


def test_the_api_is_asked_with_the_password_and_never_elsewhere() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host != "ownfoil.test":
            return httpx.Response(200, json={"data": {"apps": {"total": 0, "items": []}}})
        return httpx.Response(302, headers={"location": "http://evil.test/api/graphql"})

    with pytest.raises(UpstreamError, match="another origin"):
        list(_ownfoil(handler, user="me", password="pw").listing())
    assert [r.url.host for r in seen] == ["ownfoil.test"]
    assert seen[0].headers["authorization"].startswith("Basic ")


def test_sheets_keep_what_is_valid_and_only_images_the_shop_serves() -> None:
    bad = _title(titleId="nope", name="x")
    odd = _title(
        titleId="0100ABCD00020000", releaseDate="soon", languages=["en", "not a language", 3], category="Puzzle"
    )
    sheets = _ownfoil(_fake([], [_title(), bad, odd])).sheets()
    assert set(sheets) == {GAME, "0100ABCD00020000"}
    sheet = sheets[GAME]
    assert (sheet.name, sheet.developer, sheet.publisher, sheet.release) == (
        "Frost Garden",
        "Cold Studio",
        "Ice Press",
        "2021-03-04",
    )
    assert (sheet.genres, sheet.tags) == (("puzzle",), ("Arcade",)) and sheet.languages == ("en", "fr")
    assert sheet.icon == Picture("http://ownfoil.test:8465/api/media/x/icon/0/client/a.png", 256, 256)
    assert sheet.screenshots == ()  # the catalogue's own address is not followed
    weird = sheets["0100ABCD00020000"]
    assert (weird.release, weird.languages, weird.genres, weird.tags) == (None, ("en",), (), ())


def test_an_image_is_judged_by_its_bytes() -> None:
    assert image_kind(ICON) == "png" and image_kind(BANNER) == "jpg"
    assert image_kind(b"GIF89a....") == "gif" and image_kind(b"RIFF\0\0\0\0WEBPVP8 ") == "webp"
    assert image_kind(b"<html>not an image</html>") is None and image_kind(b"") is None
    shop = _ownfoil(lambda _: httpx.Response(200, content=b"<svg onload=alert(1)>"))
    with pytest.raises(UpstreamError):
        shop.image(Picture("http://ownfoil.test:8465/api/media/x.png"))


def test_a_task_that_ended_is_a_change() -> None:
    def event(**data: object) -> str:
        return json.dumps({"topic": "tasks", "type": "update", "data": data})

    assert _finished(event(status="completed")) and _finished(event(status="failed"))
    assert not _finished(event(status="running")) and not _finished("{") and not _finished("[]")
    assert not _finished(json.dumps({"topic": "workers", "type": "update", "data": {"status": "completed"}}))
    assert not _finished(json.dumps({"topic": "tasks", "type": "remove", "data": {"status": "completed"}}))


def test_watching_an_unreachable_shop_fails_quietly_and_stops_on_request() -> None:
    shop = Ownfoil("http://127.0.0.1:9/")
    stop, calls = threading.Event(), []
    thread = threading.Thread(target=shop.watch, args=(lambda: calls.append(1), stop))
    thread.start()
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive() and calls == []


def test_the_config_keeps_the_kind(tmp_path: Path) -> None:
    config = Config(uid=new_uid(), sources=[OwnfoilSource("of", BASE, "me", "OF_PASSWORD")])
    save(tmp_path / "config.toml", config)
    (source,) = load(tmp_path / "config.toml").sources
    assert isinstance(source, OwnfoilSource) and (source.user, source.password_env) == ("me", "OF_PASSWORD")


# -- served by the bridge ---------------------------------------------------------------------------


def _bridge(tmp_path: Path, handler: Callable[[httpx.Request], httpx.Response]) -> tuple[TestClient, Runtime]:
    config = Config(uid=new_uid(), sources=[OwnfoilSource("of", BASE)], public=True, remote_rate=0.0)
    app = make_app(config, tmp_path / "state", run=False)
    runtime: Runtime = app.state.runtime
    runtime.shops["of"] = _ownfoil(handler)
    return TestClient(app), runtime


def test_a_title_shows_ownfoils_sheet_and_images_from_the_bridge(tmp_path: Path) -> None:
    client, runtime = _bridge(tmp_path, _fake([_app()], [_title()]))
    runtime.cycle()
    assert runtime.hash_remote() is True
    runtime.cycle()  # the sheets and images were read; the app is listed
    sheet = client.get(f"/api/lantern/v1/titles/{GAME}?platform=switch").json()
    assert (sheet["name"], sheet["developer"], sheet["release"]) == ("Frost Garden", "Cold Studio", "2021-03-04")
    assert sheet["summary"] == "A garden, frozen." and sheet["genres"] == ["puzzle"] and sheet["tags"] == ["Arcade"]
    icon = sheet["icon"]
    assert icon["sha256"] == hashlib.sha256(ICON).hexdigest() and (icon["width"], icon["height"]) == (256, 256)
    assert icon["url"].startswith("/api/lantern/v1/media/") or "/v1/media/" in icon["url"]
    assert "screenshots" not in sheet
    media = client.get(icon["url"])
    assert media.status_code == 200 and media.content == ICON
    banner = client.get(sheet["banner"]["url"])
    assert banner.content == BANNER
    (app,) = client.get("/api/lantern/v1/apps?platform=switch").json()["items"]
    assert app["name"] == "Frost Garden" and app["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    runtime.index.close()


def test_a_broken_sheet_leaves_the_name(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    client, runtime = _bridge(tmp_path, _fake([_app()], [_title(releaseDate="2021-99-99")]))
    runtime.cycle()
    runtime.hash_remote()
    runtime.cycle()
    sheet = client.get(f"/api/lantern/v1/titles/{GAME}?platform=switch").json()
    assert sheet["name"] == "Frost Garden" and "developer" not in sheet
    assert "is refused" in caplog.text
    runtime.index.close()


def test_an_image_that_is_not_one_is_not_served(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/media/"):
            return httpx.Response(200, content=b"<html>login</html>")
        return _fake([_app()], [_title()])(request)

    client, runtime = _bridge(tmp_path, handler)
    runtime.cycle()
    runtime.hash_remote()
    runtime.cycle()
    sheet = client.get(f"/api/lantern/v1/titles/{GAME}?platform=switch").json()
    assert "icon" not in sheet and "banner" not in sheet
    runtime.index.close()


def test_a_shop_that_says_it_changed_is_read_again_soon(tmp_path: Path) -> None:
    listed = [_app()]
    _, runtime = _bridge(tmp_path, _fake(listed))
    runtime.cycle()
    assert runtime.index.counts() == {"hashed": 0, "pending": 1}
    listed.append(_app("0100ABCD00010800", 65536, "t2"))
    runtime.cycle()
    assert runtime.index.counts()["pending"] == 1  # the regular wait has not passed
    runtime.source_changed("of")
    runtime._shop_scanned["of"] -= CHANGED_EVERY + 1
    runtime.cycle()
    assert runtime.index.counts()["pending"] == 2
    runtime.index.close()
