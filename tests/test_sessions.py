# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The open sessions of the administration pages: seen, ended; a blocked sign-in is in the journal."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from lantern_bridge.admin.auth import AdminStore, short
from test_admin import PASSWORD, _admin, _client, _csrf


def _second(client: TestClient, agent: str = "Firefox/200") -> TestClient:
    """Another browser: its own cookie jar, the same bridge."""
    other = TestClient(
        client.app, client=("192.168.1.50", 50001), follow_redirects=False, headers={"user-agent": agent}
    )
    reply = other.post("/admin/login", data={"name": "boss", "password": PASSWORD})
    assert reply.status_code == 303
    return other


def test_the_store_lists_who_is_signed_in_and_from_where(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "admin.json")
    assert store.create(store.setup_code() or "", "boss", PASSWORD) is None
    one, _ = store.login("boss", address="10.0.0.2", agent="Firefox")
    two, _ = store.login("boss", address="10.0.0.3", agent="Safari")
    rows = store.listing(current=two)
    assert [r["address"] for r in rows] == ["10.0.0.3", "10.0.0.2"] and [r["current"] for r in rows] == [True, False]
    assert all(one not in str(r) and two not in str(r) for r in rows)  # a token is never in what the page shows
    assert store.revoke(short(one)) and not store.revoke(short(one))
    assert store.session(one) is None and store.session(two) is not None
    store.login("boss")
    assert store.revoke_others(two) == 1 and len(store.listing()) == 1


def test_the_page_lists_the_sessions_and_ends_one(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    other = _second(client)
    page = client.get("/admin/sessions").text
    assert "192.168.1.50" in page and "Firefox/200" in page and "this one" in page
    assert page.count("/end") >= 3  # one a row, and the others
    csrf = _csrf(page)
    sid = re.findall(r'action="/admin/sessions/([0-9a-f]{12})/end"', page)
    mine = client.cookies.get("lb_session") or ""
    others = [s for s in sid if s != short(mine)]
    assert len(sid) == 2 and len(others) == 1
    assert other.get("/admin/").status_code == 200
    reply = client.post(f"/admin/sessions/{others[0]}/end", data={"csrf": csrf})
    assert reply.status_code == 303 and reply.headers["location"].endswith("/sessions?m=ended")
    assert other.get("/admin/").status_code == 303  # the other browser is signed out at once
    assert client.get("/admin/sessions").status_code == 200  # this one is not
    assert client.post("/admin/sessions/000000000000/end", data={"csrf": csrf}).status_code == 404
    events = [e for e in client.app.state.runtime.history.events() if e.kind == "session_ended"]  # type: ignore[attr-defined]
    assert [e.detail for e in events] == ["1 session"]


def test_every_other_session_can_be_ended_at_once(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    first, second = _second(client), _second(client, "Safari/1")
    csrf = _csrf(client.get("/admin/sessions").text)
    assert client.post("/admin/sessions/others/end", data={"csrf": csrf}).status_code == 303
    assert first.get("/admin/").status_code == 303 and second.get("/admin/").status_code == 303
    assert client.get("/admin/").status_code == 200
    page = client.get("/admin/sessions?m=ended").text
    assert "The session was ended." in page and "/sessions/others/end" not in page  # nothing left to end
    history = client.app.state.runtime.history  # type: ignore[attr-defined]
    assert [e.detail for e in history.events() if e.kind == "session_ended"] == ["2 session(s)"]


def test_ending_your_own_session_signs_you_out(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    page = client.get("/admin/sessions").text
    mine = re.findall(r'action="/admin/sessions/([0-9a-f]{12})/end"', page)[0]
    client.post(f"/admin/sessions/{mine}/end", data={"csrf": _csrf(page)})
    assert client.get("/admin/").status_code == 303  # to the sign-in page


def test_the_sessions_are_for_the_administrator_only(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    csrf = _csrf(client.get("/admin/config").text)
    client.post(
        "/admin/operators",
        data={"csrf": csrf, "name": "viewer", "role": "observer", "password": "a long password 123"},
    )
    viewer = TestClient(client.app, client=("127.0.0.1", 50002), follow_redirects=False)
    assert viewer.post("/admin/login", data={"name": "viewer", "password": "a long password 123"}).status_code == 303
    assert viewer.get("/admin/sessions").status_code == 403
    assert viewer.post("/admin/sessions/others/end", data={"csrf": "x"}).status_code == 403
    assert "read-only" in client.get("/admin/sessions").text  # the administrator sees the viewer's session
    assert _client(tmp_path / "x").get("/admin/sessions").status_code == 303


def test_a_blocked_sign_in_is_in_the_journal_once(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _admin(client)
    stranger = TestClient(client.app, client=("10.9.9.9", 50003), follow_redirects=False)
    for _ in range(8):
        stranger.post("/admin/login", data={"name": "boss", "password": "wrong guess"})
    blocked = [e for e in client.app.state.runtime.history.events() if e.kind == "signin_blocked"]  # type: ignore[attr-defined]
    assert len(blocked) == 1 and "10.9.9.9" not in blocked[0].detail  # once, and no address in the journal
    page = client.get("/admin/events").text
    assert "Sign-ins blocked" in page and "kind=signin_blocked" in page


def test_the_texts_are_in_french_too(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    client.post("/admin/lang", data={"lang": "fr", "next": "/admin/sessions"})
    page = client.get("/admin/sessions").text
    assert "Sessions" in page and "celle-ci" in page and "propriétaire" in page
