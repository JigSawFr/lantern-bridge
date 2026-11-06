from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lantern.server import SqliteAccounts
from lantern.uid import new_uid

from lantern_bridge.app import make_app
from lantern_bridge.config import Config, FolderSource

PASSWORD = "correct horse battery"


def _client(tmp_path: Path, *, lan_only: bool = True, host: str = "127.0.0.1", sources: bool = False) -> TestClient:
    state = tmp_path / "state"
    games = tmp_path / "games"
    games.mkdir(parents=True, exist_ok=True)
    config = Config(
        uid=new_uid(),
        name="Test shelf",
        admin_lan_only=lan_only,
        sources=[FolderSource("games", games)] if sources else [],
    )
    return TestClient(make_app(config, state, run=False), client=(host, 50000), follow_redirects=False)


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _admin(client: TestClient) -> TestClient:
    """Create the administrator and sign in."""
    code = client.app.state.admin.setup_code()  # type: ignore[attr-defined]
    made = client.post("/admin/setup", data={"code": code, "name": "boss", "password": PASSWORD})
    assert made.status_code == 303
    signed = client.post("/admin/login", data={"name": "boss", "password": PASSWORD})
    assert signed.status_code == 303
    return client


@pytest.fixture(autouse=True)
def _quiet() -> Iterator[None]:
    yield
    logging.getLogger("lantern_bridge").handlers.clear()


def test_the_pages_start_with_the_setup(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/admin/").headers["location"] == "/admin/setup"
    page = client.get("/admin/setup")
    assert page.status_code == 200
    assert "Create the administrator" in page.text


def test_the_setup_needs_its_code_and_a_long_password(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.post("/admin/setup", data={"code": "0000-0000", "name": "a", "password": PASSWORD}).status_code == 400
    code = client.app.state.admin.setup_code()  # type: ignore[attr-defined]
    assert client.post("/admin/setup", data={"code": code, "name": "a", "password": "short"}).status_code == 400
    assert not (tmp_path / "state" / "admin.json").exists()


def test_the_setup_code_is_in_the_log_and_good_once(tmp_path: Path) -> None:
    client = _client(tmp_path)
    store = client.app.state.admin  # type: ignore[attr-defined]
    lines = client.app.state.logs.since(0, "WARNING")  # type: ignore[attr-defined]
    assert any(store.setup_code() in line.message for line in lines)
    _admin(client)
    assert store.setup_code() is None
    assert client.get("/admin/setup").headers["location"] == "/admin/login"
    code = "ABCD-1234"
    assert client.post("/admin/setup", data={"code": code, "name": "x", "password": PASSWORD}).status_code == 400


def test_the_admin_file_is_private_and_holds_no_password(tmp_path: Path) -> None:
    _admin(_client(tmp_path))
    path = tmp_path / "state" / "admin.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert PASSWORD not in path.read_text()
    assert json.loads(path.read_text())["schema"] == 1


def test_signing_in_is_needed_and_throttled(tmp_path: Path) -> None:
    client = _client(tmp_path)
    store = client.app.state.admin  # type: ignore[attr-defined]
    code = store.setup_code()
    client.post("/admin/setup", data={"code": code, "name": "boss", "password": PASSWORD})
    assert client.get("/admin/").headers["location"] == "/admin/login"
    for _ in range(5):
        assert client.post("/admin/login", data={"name": "boss", "password": "wrong"}).status_code == 401
    assert client.post("/admin/login", data={"name": "boss", "password": PASSWORD}).status_code == 429


def test_the_session_cookie_is_strict_and_http_only(tmp_path: Path) -> None:
    client = _client(tmp_path)
    store = client.app.state.admin  # type: ignore[attr-defined]
    client.post("/admin/setup", data={"code": store.setup_code(), "name": "boss", "password": PASSWORD})
    cookie = client.post("/admin/login", data={"name": "boss", "password": PASSWORD}).headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/admin" in cookie


def test_pages_carry_the_safety_headers(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    page = client.get("/admin/")
    assert page.status_code == 200
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert 'name="referrer" content="no-referrer"' in page.text
    assert "http://" not in page.text
    assert "https://" not in page.text  # nothing is loaded from elsewhere


def test_a_post_without_the_csrf_token_is_refused(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    assert client.post("/admin/scan", data={"action": "scan"}).status_code == 403
    token = _csrf(client.get("/admin/").text)
    assert client.post("/admin/scan", data={"action": "scan", "csrf": token}).status_code == 303


def test_the_pages_answer_the_local_network_only(tmp_path: Path) -> None:
    far = _client(tmp_path, host="8.8.8.8")
    assert far.get("/admin/login").status_code == 403
    assert far.get("/.well-known/lantern").status_code == 200  # the protocol is not behind this rule
    near = _client(tmp_path / "b", host="192.168.1.20")
    assert near.get("/admin/setup").status_code == 200
    open_ = _client(tmp_path / "c", host="8.8.8.8", lan_only=False)
    assert open_.get("/admin/setup").status_code == 200


def test_a_signed_out_request_to_the_state_is_refused(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    state = client.get("/admin/api/state")
    assert state.status_code == 200
    assert state.json()["pending"] == 0
    client.post("/admin/logout", data={"csrf": _csrf(client.get("/admin/").text)})
    assert client.get("/admin/api/state").status_code == 401


def test_the_overview_shows_the_hashing_progress(tmp_path: Path) -> None:
    client = _client(tmp_path, sources=True)
    (tmp_path / "games" / "Frost Garden [0100ABCD00010000][v0].nsp").write_bytes(b"x" * 4096)
    _admin(client)
    client.app.state.runtime.cycle(batch=0)  # type: ignore[attr-defined]
    state = client.get("/admin/api/state").json()
    assert (state["hashed"], state["pending"], state["pending_bytes"]) == (0, 1, 4096)
    assert state["sources"][0]["reachable"] is True
    client.app.state.runtime.cycle()  # type: ignore[attr-defined]
    state = client.get("/admin/api/state").json()
    assert (state["hashed"], state["pending"], state["apps"], state["titles"]) == (1, 0, 1, 1)
    page = client.get("/admin/")
    assert "Frost Garden" not in page.text  # names are not shown here
    assert "games" in page.text


def test_a_pairing_is_approved_from_the_page(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    started = client.post(
        "/api/lantern/v1/pairing", json={"schema": 1, "device": "Kitchen <b>Switch</b>", "scopes": ["library"]}
    )
    assert started.status_code in (200, 201, 202), started.text
    page = client.get("/admin/pairings")
    assert "Kitchen &lt;b&gt;Switch&lt;/b&gt;" in page.text  # escaped
    pairing_id = re.search(r'action="/admin/pairings/(pr_[^"]+)"', page.text)
    assert pairing_id
    token = _csrf(page.text)
    bad = client.post(
        f"/admin/pairings/{pairing_id.group(1)}", data={"csrf": token, "decision": "approve", "user": "bad name!"}
    )
    assert bad.status_code == 400
    done = client.post(
        f"/admin/pairings/{pairing_id.group(1)}", data={"csrf": token, "decision": "approve", "user": "family"}
    )
    assert done.status_code == 303
    devices = client.get("/admin/devices")
    assert "Kitchen &lt;b&gt;Switch&lt;/b&gt;" in devices.text
    assert "family" in devices.text
    assert client.get("/admin/api/state").json()["waiting"] == 0


def test_a_pairing_can_be_refused_and_a_device_revoked(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    client.post("/api/lantern/v1/pairing", json={"schema": 1, "device": "Den", "scopes": ["library"]})
    page = client.get("/admin/pairings")
    pairing_id = re.search(r'action="/admin/pairings/(pr_[^"]+)"', page.text).group(1)  # type: ignore[union-attr]
    token = _csrf(page.text)
    assert client.post(f"/admin/pairings/{pairing_id}", data={"csrf": token, "decision": "refuse"}).status_code == 303
    assert client.get("/admin/api/state").json()["waiting"] == 0
    # a device that an account holds, then revoked from the page
    sql = tmp_path / "state" / "accounts.sqlite3"
    with SqliteAccounts(sql) as store:
        user = store.user("family") or store.create_user("family", ("library",))
        store.issue(user.name, "Phone", ("library",))
    devices = client.get("/admin/devices")
    revoke = re.search(r'action="/admin/devices/([^"]+)/revoke"', devices.text)
    assert revoke
    assert client.post(f"/admin/devices/{revoke.group(1)}/revoke", data={"csrf": token}).status_code == 303
    assert "Phone" not in client.get("/admin/devices").text


def test_the_logs_page_lists_lines_and_filters_by_level(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    logging.getLogger("lantern_bridge.test").warning("a visible warning")
    logging.getLogger("lantern_bridge.test").info("only at info")
    page = client.get("/admin/logs?level=WARNING")
    assert "a visible warning" in page.text
    assert "only at info" not in page.text
    assert "only at info" in client.get("/admin/logs?level=INFO").text


def test_the_static_files_are_served_and_no_other(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/admin/static/app.js").status_code == 200
    assert client.get("/admin/static/app.css").headers["content-type"].startswith("text/css")
    assert client.get("/admin/static/../admin.json").status_code in (404, 307, 308)
    assert client.get("/admin/static/secret.txt").status_code == 404
