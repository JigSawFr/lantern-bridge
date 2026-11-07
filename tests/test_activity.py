"""The activity page, the traffic it counts and the developer page."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from lantern.uid import new_uid

from lantern_bridge.app import make_app
from lantern_bridge.config import Config, FolderSource, load
from lantern_bridge.traffic import Traffic
from test_admin import _admin, _client


def _dev(tmp_path: Path) -> TestClient:
    games = tmp_path / "games"
    games.mkdir(parents=True, exist_ok=True)
    config = Config(uid=new_uid(), name="Test shelf", developer=True, sources=[FolderSource("games", games)])
    return _admin(
        TestClient(make_app(config, tmp_path / "state", run=False), client=("127.0.0.1", 50000), follow_redirects=False)
    )


def test_the_server_counts_what_it_answers_and_who_asked(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    traffic: Traffic = client.app.state.runtime.traffic  # type: ignore[attr-defined]
    assert traffic.totals.requests == 0  # the administration pages are not the library's traffic
    reply = client.get("/.well-known/lantern", headers={"User-Agent": "TestSwitch/1.0"})
    assert reply.status_code == 200
    assert traffic.totals.requests == 1 and traffic.totals.sent >= len(reply.content)
    transfers, clients, recent = traffic.snapshot()
    assert not transfers  # nothing is left running
    assert [c.agent for c in clients] == ["TestSwitch/1.0"] and clients[0].requests == 1
    assert recent[0].path == "/.well-known/lantern" and recent[0].status == 200
    assert traffic.rates()["requests"] > 0


def test_the_table_of_machines_does_not_grow_without_end() -> None:
    traffic = Traffic()
    for n in range(250):
        traffic.end(traffic.begin(f"10.0.{n // 250}.{n % 250}", "GET", "/x", None), "")
    assert len(traffic.clients) == 200 and traffic.totals.requests == 250


def test_the_activity_page_and_its_live_block(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    client.get("/.well-known/lantern", headers={"User-Agent": "TestSwitch/1.0"})
    page = client.get("/admin/activity")
    assert (
        page.status_code == 200
        and "Transfers in progress" in page.text
        and 'data-live="/admin/activity?fragment=1"' in page.text
    )
    assert "TestSwitch/1.0" in page.text
    fragment = client.get("/admin/activity?fragment=1")
    assert "<html" not in fragment.text and "Machines seen" in fragment.text
    client.post("/admin/lang", data={"lang": "fr", "next": "/admin/activity"})
    assert "Transferts en cours" in client.get("/admin/activity").text
    assert _client(tmp_path / "other").get("/admin/activity").status_code == 303  # needs the administrator


def test_the_developer_page_is_off_unless_asked(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    assert client.get("/admin/developer").status_code == 404
    assert client.post("/admin/developer/scan").status_code in (403, 404)
    assert "/admin/developer" not in client.get("/admin/").text


def test_the_developer_page_shows_requests_timings_and_raw_json(tmp_path: Path) -> None:
    client = _dev(tmp_path)
    games = tmp_path / "games"
    (games / "Aurora Quest [0100AAAA00000000][v0].nsp").write_bytes(b"base")
    client.app.state.runtime.cycle(batch=5)  # type: ignore[attr-defined]
    client.get("/.well-known/lantern")
    page = client.get("/admin/developer")
    assert page.status_code == 200 and "/.well-known/lantern" in page.text and "Aurora Quest" in page.text
    assert "/admin/developer" in client.get("/admin/").text
    raw = client.get("/admin/developer/title.json?id=0100aaaa00000000")
    body = raw.json()
    assert (
        raw.status_code == 200 and body["known"] and body["files"][0]["type"] == "base" and body["apps"][0]["title_id"]
    )
    assert client.get("/admin/developer/title.json?id=zz").json()["known"] is False
    token = page.text.split('name="csrf" value="')[1].split('"')[0]
    assert client.post("/admin/developer/forget", data={"csrf": token}, follow_redirects=False).status_code == 303
    assert client.post("/admin/developer/nonsense", data={"csrf": token}).status_code == 404


def test_the_developer_setting_is_saved_and_off_by_default(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    path = tmp_path / "state" / "config.toml"
    page = client.get("/admin/config")
    token = page.text.split('name="csrf" value="')[1].split('"')[0]
    assert "developer = false" in client.app.state.runtime.config.to_toml()  # type: ignore[attr-defined]
    done = client.post(
        "/admin/config/settings",
        data={"csrf": token, "name": "Shelf", "remote_rate": "10", "developer": "on", "admin_lan_only": "on"},
    )
    assert done.status_code in (200, 303) and load(path).developer is True
