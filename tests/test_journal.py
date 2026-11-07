"""The journal: the events worth a line, kept across restarts, with no path and no title."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from lantern_bridge.admin.i18n import FR
from lantern_bridge.history import KEEP_DAYS, MAX_EVENTS, History
from lantern_bridge.runtime import Runtime, SourceState
from lantern_bridge.updates import Updates
from test_admin import _admin, _client, _csrf
from test_observer import _with_observer


def _runtime(client: TestClient) -> Runtime:
    runtime: Runtime = client.app.state.runtime  # type: ignore[attr-defined]
    return runtime


def test_events_are_kept_newest_first_and_cut_by_kind(tmp_path: Path) -> None:
    history = History(tmp_path / "a.sqlite3")
    base = time.time() - 1000
    history.event("scan", "games", "+1 ~0 -0", now=base + 100)
    history.event("settings", detail="saved", now=base + 200)
    history.event("scan", "games", "+0 ~2 -0", now=base + 300)
    assert [e.detail for e in history.events()] == ["+0 ~2 -0", "saved", "+1 ~0 -0"]
    assert [e.ts for e in history.events(kind="scan")] == [base + 300, base + 100]
    assert len(history.events(limit=1)) == 1
    history.close()
    assert [e.kind for e in History(tmp_path / "a.sqlite3").events()] == ["scan", "settings", "scan"]  # across restarts


def test_old_and_surplus_events_are_dropped_and_a_long_detail_is_cut(tmp_path: Path) -> None:
    path = tmp_path / "a.sqlite3"
    history = History(path)
    history.event("scan", "x" * 200, "y" * 500, now=time.time() - (KEEP_DAYS + 1) * 86400)
    history.event("scan", "x" * 200, "y" * 500)
    history.close()
    again = History(path)  # opening prunes
    kept = again.events()
    assert len(kept) == 1 and len(kept[0].subject) == 64 and len(kept[0].detail) == 120
    for n in range(MAX_EVENTS + 5):
        again.event("scan", detail=str(n), now=time.time() - 1 + n * 1e-6)
    again.close()
    assert len(History(path).events(limit=5000)) <= MAX_EVENTS


def test_a_file_of_a_newer_schema_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "a.sqlite3"
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 99")
    db.commit()
    db.close()
    history = History(path)
    history.event("scan")  # nothing happens, no error
    assert not history.usable and history.events() == []


def test_the_bridge_writes_its_turning_points_not_every_pass(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    runtime = _runtime(client)
    runtime.start()
    try:
        (tmp_path / "games" / "Calm Tide [0100CCCC00000000][v0].nsp").write_bytes(b"fine")
        runtime.cycle(batch=10)
        state = runtime.stats.sources["games"]
        runtime._reach("games", state, OSError(2, "No such file or directory", "/secret/place/games"))
        runtime._reach("games", state, OSError(2, "No such file or directory", "/secret/place/games"))  # still down
        runtime._reach("games", state, None)
        runtime._reach("games", state, None)  # still up
    finally:
        runtime._stop.set()
    kinds = [e.kind for e in reversed(runtime.history.events())]
    assert kinds[0] == "started" and kinds.count("source_down") == 1 and kinds.count("source_up") == 1
    assert "scan" in kinds
    assert all("/secret" not in e.detail and "Calm Tide" not in e.detail for e in runtime.history.events())
    down = next(e for e in runtime.history.events() if e.kind == "source_down")
    assert down.subject == "games" and down.detail == "No such file or directory"


def test_a_shop_that_does_not_answer_is_told_by_its_own_message(tmp_path: Path) -> None:
    from lantern_bridge.sources.tinfoil import UpstreamError

    client = _admin(_client(tmp_path, sources=True))
    runtime = _runtime(client)
    runtime._reach("games", SourceState(), UpstreamError("the shop answered 502"))
    assert runtime.history.events()[0].detail == "the shop answered 502"


def test_a_newer_release_is_noted_once(tmp_path: Path) -> None:
    import httpx

    seen: list[str] = []
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"tag_name": "v9.0.0"})))
    updates = Updates(current="0.1.0", client=client, notify=seen.append)
    updates.check()
    updates.check()  # the same release again: nothing new
    assert seen == ["v9.0.0"]
    quiet = Updates(current="99.0.0", client=client, notify=seen.append)
    quiet.check()  # not newer than what runs
    assert seen == ["v9.0.0"]


def test_the_page_lists_events_and_the_settings_leave_a_line(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    empty = client.get("/admin/events")
    assert empty.status_code == 200 and "Nothing yet" in empty.text
    csrf = _csrf(client.get("/admin/config").text)
    form = {"csrf": csrf, "remote_rate": "10", "admin_lan_only": "on"}
    client.post("/admin/config/settings", data={**form, "name": "One"})
    client.post("/admin/config/settings", data={**form, "name": "Two"})
    client.post("/admin/config/undo", data={"csrf": csrf})
    page = client.get("/admin/events").text
    assert page.count("Settings saved") >= 4  # the chip, and a line for each save and for the undo
    assert "saved" in page and "undone" in page
    only = client.get("/admin/events?kind=scan").text
    assert "Nothing of this kind yet." in only
    assert "Nothing of this kind yet." not in client.get("/admin/events?kind=bogus").text  # an unknown kind: all


def test_the_journal_is_for_the_administrator_only(tmp_path: Path) -> None:
    boss, eye = _with_observer(tmp_path)
    assert boss.get("/admin/events").status_code == 200
    assert eye.get("/admin/events").status_code == 403
    assert "Journal" not in eye.get("/admin/").text


def test_the_journal_texts_have_their_french() -> None:
    for text in (
        "Journal",
        "Bridge started",
        "Files changed",
        "Source down",
        "Source back",
        "Newer version out",
        "Settings saved",
        "Device paired",
        "Pairing refused",
        "Device revoked",
        "Nothing of this kind yet.",
    ):
        assert text in FR
