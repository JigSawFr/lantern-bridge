"""What keeps one visitor from wearing the administration out: tries per account, idle sessions, open streams."""

from __future__ import annotations

from pathlib import Path

from test_admin import PASSWORD, _admin, _client


def test_a_name_is_throttled_across_addresses_but_widely_enough_not_to_lock_the_owner_out(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    pages = client.app.state.admin_pages  # type: ignore[attr-defined]
    for i in range(6):  # six strangers, each from its own address, each failing once
        pages.attempts.record(f"10.0.0.{i}")
    for _ in range(3):
        pages.accounts.record("name:boss")
    assert client.post("/admin/login", data={"name": "boss", "password": PASSWORD}).status_code == 303
    for _ in range(30):
        pages.accounts.record("name:boss")
    refused = client.post("/admin/login", data={"name": "Boss", "password": PASSWORD})
    assert refused.status_code == 429  # the name is counted whatever its case


def test_a_wrong_password_counts_against_the_address_and_the_name(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    pages = client.app.state.admin_pages  # type: ignore[attr-defined]
    client.post("/admin/login", data={"name": "boss", "password": "wrong password"})
    assert pages.accounts._hits["name:boss"] and pages.attempts._hits["127.0.0.1"]


def test_an_idle_session_ends_and_a_page_asking_again_by_itself_does_not_keep_it_alive(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    store = client.app.state.admin  # type: ignore[attr-defined]
    (session,) = store._sessions.values()
    token = next(iter(store._sessions))
    before = session.seen
    assert store.session(token, touch=False) is session and session.seen == before
    assert store.session(token) is session and session.seen >= before
    session.seen -= 31 * 60
    assert store.session(token, touch=False) is None  # asked for by itself: it stays idle and ends
    assert client.get("/admin/").headers["location"] == "/admin/login"


def test_a_session_may_keep_a_few_streams_open_and_no_more(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    pages = client.app.state.admin_pages  # type: ignore[attr-defined]
    cookie = client.cookies.get("lb_session")
    assert cookie
    pages.streams[cookie] = 8
    assert client.get("/admin/live?u=/admin/api/state").status_code == 429
