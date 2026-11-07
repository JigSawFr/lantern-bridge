from __future__ import annotations

import contextlib
import hashlib
import json
import random
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from lantern.server import MemoryLibrary
from lantern.uid import new_uid

from lantern_bridge.app import make_app
from lantern_bridge.config import Config, FolderSource, Source, TinfoilSource
from lantern_bridge.runtime import Runtime
from lantern_bridge.sources import tinfoil
from lantern_bridge.sources.tinfoil import (
    Listed,
    Shop,
    UpstreamError,
    depth,
    directories,
    entries,
    parse_index,
    safe_name,
)

BASE = "http://shop.test/"
NAME = "Frost Garden [0100ABCD00010000][v0].nsp"
PAYLOAD = b"frost" * 2000


def _shop(handler: Callable[[httpx.Request], httpx.Response], url: str = BASE, **kw: str) -> Shop:
    return Shop(url, client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def _json(document: object) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(document).encode(), headers={"content-type": "application/json"})


def test_entries_are_read_against_the_index_and_named() -> None:
    document = {
        "files": [
            f"{BASE}games/{NAME.replace(' ', '%20')}",
            {"url": "dl?id=7#Dune Moss [0100ABCD00020000][v65536].nsp", "size": 12},
            {"url": "games/no-size.nsp"},
        ]
    }
    # a bare URL has no size and is dropped; an object with a size is kept
    listed = list(entries(document, BASE))
    assert [(f.name, f.size) for f in listed] == [("Dune Moss [0100ABCD00020000][v65536].nsp", 12)]
    assert listed[0].url == "http://shop.test/dl?id=7"


def test_a_file_of_another_origin_is_never_listed() -> None:
    document = {
        "files": [
            {"url": "http://evil.test/a [0100ABCD00010000][v0].nsp", "size": 1},
            {"url": "https://shop.test/a [0100ABCD00010000][v0].nsp", "size": 1},  # another scheme and port
            {"url": "http://shop.test:8080/a [0100ABCD00010000][v0].nsp", "size": 1},
            {"url": "http://user:pw@shop.test/a [0100ABCD00010000][v0].nsp", "size": 1},
            {"url": "file:///etc/passwd", "size": 1},
            {"url": "http://shop.test/ok [0100ABCD00010000][v0].nsp", "size": 1},
        ]
    }
    assert [f.name for f in entries(document, BASE)] == ["ok [0100ABCD00010000][v0].nsp"]
    assert directories({"directories": ["http://evil.test/", "/sub/", {"url": "other/"}]}, BASE) == [
        "http://shop.test/sub/",
        "http://shop.test/other/",
    ]


@pytest.mark.parametrize("name", ["", ".", "..", "a/b.nsp", "a\\b.nsp", "a\x00b.nsp", "a\nb.nsp", "x" * 300])
def test_a_name_that_would_leave_a_folder_is_dropped(name: str) -> None:
    assert safe_name(name) is None


def test_the_index_is_bounded_in_depth_and_shape() -> None:
    assert depth({"a": [[[1]]]}) == 4
    nested: object = 1
    for _ in range(tinfoil.MAX_DEPTH + 5):
        nested = [nested]
    with pytest.raises(UpstreamError, match="deeper"):
        parse_index(json.dumps({"files": nested}).encode())
    with pytest.raises(UpstreamError):
        parse_index(b"[1, 2]")
    with pytest.raises(UpstreamError):
        parse_index(b"{nope")
    with pytest.raises(UpstreamError):  # past what the parser itself can nest
        parse_index(b"[" * 100_000 + b"]" * 100_000)


def test_a_huge_index_is_cut() -> None:
    shop = _shop(lambda request: httpx.Response(200, content=b"{" + b" " * 2048))
    with pytest.raises(UpstreamError, match="larger"):
        shop.get(BASE, limit=1024)


def test_a_redirect_is_followed_inside_the_origin_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "/index.json"})
        if request.url.path == "/index.json":
            return _json({"files": []})
        return httpx.Response(404)

    assert _shop(handler).get(BASE) == b'{"files": []}'

    away = _shop(lambda request: httpx.Response(302, headers={"location": "http://169.254.169.254/latest"}))
    with pytest.raises(UpstreamError, match="another origin"):
        away.get(BASE)
    loop = _shop(lambda request: httpx.Response(302, headers={"location": "/"}))
    with pytest.raises(UpstreamError, match="redirects"):
        loop.get(BASE)


def test_errors_say_what_happened_without_the_password() -> None:
    shop = _shop(lambda request: httpx.Response(401), user="u", password="hunter2")
    with pytest.raises(UpstreamError) as error:
        shop.get(BASE)
    assert "401" in str(error.value) and "hunter2" not in str(error.value)

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(UpstreamError, match="cannot be reached"):
        _shop(broken).get(BASE)


def test_basic_auth_is_sent_to_the_shop() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return _json({"files": []})

    _shop(handler, user="u", password="p").get(BASE)
    assert seen and seen[0] and seen[0].startswith("Basic ")


def test_directories_are_followed_once_and_a_cycle_ends() -> None:
    pages = {
        "/": {"files": [{"url": f"a/{NAME}", "size": 1}], "directories": ["/b/", "/b/"]},
        "/b/": {"files": [{"url": f"b/{NAME}", "size": 2}], "directories": ["/", "/b/", "http://evil.test/"]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page = pages.get(request.url.path)
        return _json(page) if page is not None else httpx.Response(404)

    assert [f.size for f in _shop(handler).listing()] == [1, 2]


def test_a_broken_directory_is_skipped_but_a_broken_index_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return _json({"files": [{"url": NAME, "size": 1}], "directories": ["/gone/"]})
        return httpx.Response(500)

    assert len(list(_shop(handler).listing())) == 1
    with pytest.raises(UpstreamError):
        list(_shop(lambda request: httpx.Response(500)).listing())


def test_scan_keeps_the_packages_the_names_identify() -> None:
    document = {
        "files": [
            {"url": f"games/{NAME}", "size": 10},
            {"url": "games/readme.txt", "size": 1},
            {"url": "games/Unknown Game.nsp", "size": 1},
        ]
    }
    found = list(_shop(lambda request: _json(document)).scan())
    assert [(e.path, e.size, e.mtime_ns) for e in found] == [(f"games/{NAME}#{NAME}", 10, 0)]


def test_an_index_key_gives_its_address_back() -> None:
    shop = _shop(lambda request: httpx.Response(404), "http://shop.test:8080/base/")
    listed = Listed("http://shop.test:8080/dl%20x/a%2Fb?id=3&k=v", "n.nsp", 1)
    key = tinfoil.index_path(listed)
    assert key == "dl%20x/a%2Fb?id=3&k=v#n.nsp"
    assert shop.address(key) == listed.url


def test_stream_reads_a_range_and_notices_a_short_file() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        wanted = request.headers.get("range")
        if wanted is None:
            return httpx.Response(200, content=PAYLOAD)
        first, last = (int(x) for x in wanted.removeprefix("bytes=").split("-"))
        return httpx.Response(206, content=PAYLOAD[first : last + 1])

    shop = _shop(handler)
    assert b"".join(shop.stream("f.nsp#f.nsp")) == PAYLOAD
    assert b"".join(shop.stream("f.nsp#f.nsp", 100, 50)) == PAYLOAD[100:150]
    short = _shop(lambda request: httpx.Response(206, content=b"abc"))
    with pytest.raises(UpstreamError, match="early"):
        b"".join(short.stream("f.nsp#f.nsp", 0, 10))
    # a server that ignores the Range and sends everything is cut to what was asked for from the start
    ignoring = _shop(lambda request: httpx.Response(200, content=PAYLOAD))
    assert b"".join(ignoring.stream("f.nsp#f.nsp", 0, 5)) == PAYLOAD[:5]
    with pytest.raises(UpstreamError, match="answered 200"):  # from the middle that would be the wrong bytes
        b"".join(ignoring.stream("f.nsp#f.nsp", 5, 5))


def _random_json(rng: random.Random, level: int = 0) -> object:
    kind = rng.randrange(8 if level < 6 else 5)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice([True, False])
    if kind == 2:
        return rng.randrange(-5, 10**12)
    if kind == 3:
        return rng.choice(["", "a", NAME, "../x", "http://evil.test/x", "/a/b\x00", "x" * 300, "#", "%2e%2e/"])
    if kind == 4:
        return rng.random()
    if kind == 5:
        return [_random_json(rng, level + 1) for _ in range(rng.randrange(4))]
    keys = ["files", "directories", "url", "size", "titledb", "success", "x"]
    return {rng.choice(keys): _random_json(rng, level + 1) for _ in range(rng.randrange(5))}


def test_a_hostile_index_never_breaks_the_reader() -> None:
    """Random documents of every shape: the reader yields only safe, same-origin entries and raises nothing else."""
    rng = random.Random(20261106)
    for _ in range(3000):
        document = _random_json(rng)
        if not isinstance(document, dict):
            document = {"files": document, "directories": document}
        for listed in entries(document, BASE):
            assert tinfoil.origin(listed.url) == tinfoil.origin(BASE)
            assert safe_name(listed.name) == listed.name and listed.size >= 0
        for address in directories(document, BASE):
            assert tinfoil.origin(address) == tinfoil.origin(BASE)
    for _ in range(500):
        blob = bytes(rng.randrange(256) for _ in range(rng.randrange(200)))
        with contextlib.suppress(UpstreamError):
            parse_index(blob)


# -- the runtime hashes a shop's files by reading them --------------------------------------------


def _runtime(tmp_path: Path, handler: Callable[[httpx.Request], httpx.Response], rate: float = 0.0) -> Runtime:
    config = Config(uid="0c9c2a8e-5d3a-4c7e-8b0a-6f1d2e3c4b5a", sources=[TinfoilSource("shop", BASE)], remote_rate=rate)
    runtime = Runtime(config, tmp_path / "state", MemoryLibrary())
    runtime.shops["shop"] = _shop(handler)
    return runtime


def _shop_handler(payload: bytes = PAYLOAD, size: int | None = None) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return _json({"files": [{"url": f"games/{NAME}", "size": len(payload) if size is None else size}]})
        return httpx.Response(200, content=payload)

    return handler


def test_a_shops_file_is_hashed_by_reading_it(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, _shop_handler())
    runtime.cycle()
    assert runtime.index.counts() == {"hashed": 0, "pending": 1}
    assert runtime.hash_remote() is True
    assert runtime.index.counts() == {"hashed": 1, "pending": 0}
    (row,) = runtime.index.hashed("shop")
    assert row.sha256 == hashlib.sha256(PAYLOAD).hexdigest()
    assert runtime.stats.hashed_bytes == len(PAYLOAD)
    assert runtime.hash_remote() is False
    runtime.index.close()


def test_a_file_that_is_not_the_size_the_shop_said_is_not_hashed(tmp_path: Path) -> None:
    for wrong in (len(PAYLOAD) - 1, len(PAYLOAD) + 1):
        runtime = _runtime(tmp_path / str(wrong), _shop_handler(size=wrong))
        runtime.cycle()
        assert runtime.hash_remote() is False
        assert runtime.index.counts() == {"hashed": 0, "pending": 1}
        assert runtime.hash_remote() is False  # not tried again at once
        runtime.index.close()


def test_a_shop_that_cannot_be_read_is_reported_not_fatal(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, lambda request: httpx.Response(503))
    runtime.cycle()
    state = runtime.stats.sources["shop"]
    assert state.reachable is False and "503" in (state.error or "")
    runtime.index.close()


def test_local_files_are_hashed_even_when_shop_files_come_first(tmp_path: Path) -> None:
    games = tmp_path / "games"
    games.mkdir()
    (games / "Local Game [0100ABCD00030000][v0].nsp").write_bytes(b"x" * 100)
    config = Config(
        uid="0c9c2a8e-5d3a-4c7e-8b0a-6f1d2e3c4b5a", sources=[TinfoilSource("shop", BASE), FolderSource("games", games)]
    )
    runtime = Runtime(config, tmp_path / "state", MemoryLibrary())
    runtime.shops["shop"] = _shop(_shop_handler())
    assert runtime.cycle() == 1  # the shop's file waits for the other thread; the local one is not starved
    assert runtime.index.counts_by_source()["games"]["hashed"] == 1
    runtime.index.close()


# -- served by the bridge: relayed, merged ---------------------------------------------------------


def _ranged(payload: bytes) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        wanted = request.headers.get("range")
        if wanted is None:
            return httpx.Response(200, content=payload)
        first, _, last = wanted.removeprefix("bytes=").partition("-")
        return httpx.Response(206, content=payload[int(first) : int(last) + 1 if last else None])

    return handler


def _bridge(
    tmp_path: Path, sources: list[Source], shops: dict[str, Callable[[httpx.Request], httpx.Response]]
) -> tuple[TestClient, Runtime]:
    app = make_app(Config(uid=new_uid(), sources=sources, public=True, remote_rate=0.0), tmp_path / "state", run=False)
    runtime: Runtime = app.state.runtime
    for source, handler in shops.items():
        runtime.shops[source] = _shop(handler)
    return TestClient(app), runtime


def _items(client: TestClient) -> list[dict[str, object]]:
    return client.get("/api/lantern/v1/apps?platform=switch").json()["items"]  # type: ignore[no-any-return]


def test_a_shops_file_is_served_by_the_bridge_with_its_ranges(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return _json({"files": [{"url": f"games/{NAME}", "size": len(PAYLOAD)}]})
        return _ranged(PAYLOAD)(request)

    client, runtime = _bridge(tmp_path, [TinfoilSource("shop", BASE)], {"shop": handler})
    with client:
        runtime.cycle()
        assert _items(client) == []  # not listed until hashed
        assert runtime.hash_remote()
        runtime.cycle()
        (item,) = _items(client)
        assert item["sha256"] == hashlib.sha256(PAYLOAD).hexdigest() and item["size"] == len(PAYLOAD)
        whole = client.get(str(item["file"]))
        assert whole.status_code == 200 and whole.content == PAYLOAD
        assert whole.headers["content-length"] == str(len(PAYLOAD))
        assert whole.headers["repr-digest"].startswith("sha-256=:")
        part = client.get(str(item["file"]), headers={"Range": "bytes=10-19"})
        assert part.status_code == 206 and part.content == PAYLOAD[10:20]
        assert part.headers["content-range"] == f"bytes 10-19/{len(PAYLOAD)}"
        assert client.head(str(item["file"])).headers["content-length"] == str(len(PAYLOAD))


def test_the_same_bytes_in_two_sources_are_one_app_served_by_the_folder(tmp_path: Path) -> None:
    games = tmp_path / "games"
    games.mkdir()
    local = games / NAME
    local.write_bytes(PAYLOAD)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/":
            return _json({"files": [{"url": f"games/{NAME}", "size": len(PAYLOAD)}]})
        return _ranged(PAYLOAD)(request)

    # the shop comes first in the configuration, and still the folder is preferred: it is local
    sources: list[Source] = [TinfoilSource("shop", BASE), FolderSource("games", games)]
    client, runtime = _bridge(tmp_path, sources, {"shop": handler})
    with client:
        runtime.cycle()
        runtime.hash_remote()
        runtime.cycle()
        (item,) = _items(client)
        assert client.get(str(item["file"])).content == PAYLOAD
        served = [c for c in calls if c != "/"]
        assert len(served) == 1  # the one read that hashed it; serving came from the folder
        # the folder file goes: the shop serves the same app, which stays listed and keeps its id
        local.unlink()
        runtime.cycle()
        (after,) = _items(client)
        assert after["sha256"] == item["sha256"]
        assert client.get(str(after["file"])).content == PAYLOAD
        assert len([c for c in calls if c != "/"]) == 2  # now it was read from the shop


def test_when_the_best_folder_vanishes_another_serves_the_app(tmp_path: Path) -> None:
    games = tmp_path / "games"
    games.mkdir()
    (games / NAME).write_bytes(PAYLOAD)
    other = tmp_path / "more"
    other.mkdir()
    (other / NAME).write_bytes(PAYLOAD)
    client, runtime = _bridge(tmp_path, [FolderSource("games", games), FolderSource("more", other)], {})
    with client:
        runtime.cycle()
        assert len(_items(client)) == 1
        (games / NAME).unlink()
        runtime.cycle()
        (item,) = _items(client)
        assert client.get(str(item["file"])).content == PAYLOAD  # now served from the other folder


def test_every_call_names_the_bridge_as_a_tinfoil_client() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent", ""))
        return httpx.Response(200, content=b"{}")

    shop = Shop("http://shop.example.org/", client=httpx.Client(transport=httpx.MockTransport(handle)))
    shop.get("http://shop.example.org/")
    list(shop.stream("a.nsp"))
    assert seen
    assert all("tinfoil" in agent.lower() for agent in seen)
