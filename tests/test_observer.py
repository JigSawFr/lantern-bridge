"""The read-only operator: it looks at a few pages and changes nothing."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_admin import _admin, _client

SEE = "watcher-pass-1234"


def _csrf(client: TestClient, path: str = "/admin/config") -> str:
    found = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
    assert found
    return found.group(1)


def _with_observer(tmp_path: Path) -> tuple[TestClient, TestClient]:
    boss = _admin(_client(tmp_path, sources=True))
    reply = boss.post(
        "/admin/operators", data={"csrf": _csrf(boss), "name": "watcher", "role": "observer", "password": SEE}
    )
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
        "/admin/operators",
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
    assert reply.status_code == 403 and "Not for this account" in reply.text


def test_the_observer_cannot_read_the_log_stream(tmp_path: Path) -> None:
    _, eye = _with_observer(tmp_path)
    assert eye.get("/admin/logs/stream").status_code == 401  # as for anyone refused: an event stream has no page


def test_the_observer_cannot_change_anything_even_with_its_own_csrf_token(tmp_path: Path) -> None:
    boss, eye = _with_observer(tmp_path)
    token = _csrf(eye, "/admin/")
    for path, data in (
        ("/admin/scan", {"action": "rehash"}),
        ("/admin/config/settings", {"name": "Hacked", "remote_rate": "1"}),
        ("/admin/operators", {"name": "other", "role": "owner", "password": SEE + "x"}),
        ("/admin/config/backup", {}),
    ):
        reply = eye.post(path, data={"csrf": token, **data})
        assert reply.status_code == 403, path
    assert boss.app.state.runtime.config.name != "Hacked"  # type: ignore[attr-defined]
    assert eye.post("/admin/logout", data={"csrf": token}).status_code == 303  # leaving is allowed
    assert eye.get("/admin/").status_code == 303
