"""The read-only account: it looks at a few pages and changes nothing."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lantern_bridge.admin.auth import AdminStore
from test_admin import PASSWORD, _admin, _client

SEE = "watcher-pass-1234"


def _csrf(client: TestClient, path: str = "/admin/config") -> str:
    found = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
    assert found
    return found.group(1)


def _with_observer(tmp_path: Path) -> tuple[TestClient, TestClient]:
    boss = _admin(_client(tmp_path, sources=True))
    reply = boss.post("/admin/config/observer", data={"csrf": _csrf(boss), "name": "watcher", "password": SEE})
    assert reply.status_code == 303
    eye = TestClient(boss.app, client=("127.0.0.1", 50001), follow_redirects=False)
    assert eye.post("/admin/login", data={"name": "watcher", "password": SEE}).status_code == 303
    return boss, eye


def test_the_observer_sees_the_overview_the_library_and_the_activity(tmp_path: Path) -> None:
    _, eye = _with_observer(tmp_path)
    for page in ("/admin/", "/admin/library", "/admin/library/checks", "/admin/activity", "/admin/api/state"):
        assert eye.get(page).status_code == 200, page
    html = eye.get("/admin/").text
    menu = html.split("<nav", 1)[1].split("</nav>", 1)[0]  # the menu offers only what the account may open
    assert 'href="/admin/library"' in menu and 'href="/admin/activity"' in menu
    for hidden in ("config", "pairings", "devices", "accounts", "invitations", "logs", "developer"):
        assert f'href="/admin/{hidden}"' not in menu, hidden
    assert "Sign out" in html


@pytest.mark.parametrize(
    "path",
    [
        "/admin/config",
        "/admin/pairings",
        "/admin/devices",
        "/admin/accounts",
        "/admin/invitations",
        "/admin/logs",
        "/admin/developer",
    ],
)
def test_the_observer_cannot_open_the_rest(tmp_path: Path, path: str) -> None:
    _, eye = _with_observer(tmp_path)
    reply = eye.get(path)
    assert reply.status_code == 403 and "Read-only account" in reply.text


def test_the_observer_cannot_read_the_log_stream(tmp_path: Path) -> None:
    _, eye = _with_observer(tmp_path)
    assert eye.get("/admin/logs/stream").status_code == 401  # as for anyone refused: an event stream has no page


def test_the_observer_cannot_change_anything_even_with_its_own_csrf_token(tmp_path: Path) -> None:
    boss, eye = _with_observer(tmp_path)
    token = _csrf(eye, "/admin/")
    for path, data in (
        ("/admin/scan", {"action": "rehash"}),
        ("/admin/config/settings", {"name": "Hacked", "remote_rate": "1"}),
        ("/admin/config/observer", {"name": "other", "password": SEE + "x"}),
        ("/admin/config/backup", {}),
    ):
        reply = eye.post(path, data={"csrf": token, **data})
        assert reply.status_code == 403, path
    assert boss.app.state.runtime.config.name != "Hacked"  # type: ignore[attr-defined]
    assert eye.post("/admin/logout", data={"csrf": token}).status_code == 303  # leaving is allowed
    assert eye.get("/admin/").status_code == 303


def test_the_names_must_differ_and_the_password_be_long(tmp_path: Path) -> None:
    boss = _admin(_client(tmp_path))
    token = _csrf(boss)
    for data in (
        {"name": "boss", "password": SEE},
        {"name": "BOSS", "password": SEE},
        {"name": "w", "password": "short"},
        {"name": "", "password": SEE},
    ):
        assert boss.post("/admin/config/observer", data={"csrf": token, **data}).status_code == 400, data
    assert AdminStore(tmp_path / "state" / "admin.json").observer is None


def test_removing_it_closes_its_sessions_and_its_password_stops_working(tmp_path: Path) -> None:
    boss, eye = _with_observer(tmp_path)
    assert "watcher" in boss.get("/admin/config").text
    assert boss.post("/admin/config/observer", data={"csrf": _csrf(boss), "action": "remove"}).status_code == 303
    assert eye.get("/admin/").status_code == 303  # its session is gone
    other = TestClient(boss.app, client=("127.0.0.1", 50002), follow_redirects=False)
    assert other.post("/admin/login", data={"name": "watcher", "password": SEE}).status_code == 401


def test_the_administrator_is_still_the_administrator(tmp_path: Path) -> None:
    boss, _ = _with_observer(tmp_path)
    assert boss.get("/admin/config").status_code == 200
    assert boss.post("/admin/login", data={"name": "boss", "password": PASSWORD}).status_code == 303
    store = AdminStore(tmp_path / "state" / "admin.json")
    assert store.identify("boss", PASSWORD) == "admin" and store.identify("watcher", SEE) == "observer"
    assert store.identify("watcher", PASSWORD) is None and store.identify("boss", SEE) is None
    assert store.check("watcher", SEE) is False


def test_the_observer_is_kept_when_the_file_is_read_by_an_older_shape(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "admin.json")
    assert store.observer is None and store.set_observer("w", SEE) == "There is no administrator yet."
