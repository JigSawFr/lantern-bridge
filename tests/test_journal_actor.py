# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The journal says who did it: the operator's name on every line made from the pages, none on the bridge's own."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from pathlib import Path

from tests.test_admin import _admin, _client
from tests.test_roles import _csrf

from lantern_bridge.history import History


def test_a_line_keeps_who_did_it_and_the_bridge_s_own_lines_keep_nobody(tmp_path: Path) -> None:
    history = History(tmp_path / "activity.sqlite3")
    seen = []
    history.listeners.append(seen.append)
    history.event("started", detail="1.0")
    history.event("paired", "lea", "Living room", actor="Alex")
    newest, oldest = history.events(2)
    assert (newest.actor, newest.subject, newest.detail) == ("Alex", "lea", "Living room")
    assert oldest.actor == "" and [e.actor for e in seen] == ["", "Alex"]
    assert [e.actor for e in history.events(5, "paired")] == ["Alex"]
    history.close()


def test_a_journal_file_from_before_the_actor_is_taken_up_once(tmp_path: Path) -> None:
    path = tmp_path / "activity.sqlite3"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE events (ts REAL, kind TEXT, subject TEXT, detail TEXT);"
        "INSERT INTO events VALUES (strftime('%s','now'), 'scan', 'games', '+1');PRAGMA user_version = 1;"
    )
    old.commit()
    old.close()
    history = History(path)
    assert [(e.kind, e.actor) for e in history.events()] == [("scan", "")]
    history.event("settings", actor="Alex")
    assert history.events(1)[0].actor == "Alex"
    history.close()


def test_what_an_operator_does_from_the_pages_is_signed_and_exported(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    done = client.post(
        "/admin/accounts", data={"csrf": _csrf(client, "/admin/accounts"), "name": "lea", "preset": "full"}
    )
    assert done.status_code in (200, 303)
    events = client.app.state.runtime.history.events()  # type: ignore[attr-defined]
    added = next(e for e in events if e.kind == "account_added")
    assert added.subject == "lea" and added.actor == "boss"
    page = client.get("/admin/events").text
    assert "· boss" in page
    rows = list(csv.reader(io.StringIO(client.get("/admin/events/export?format=csv").text)))
    assert rows[0] == ["time", "kind", "subject", "detail", "actor"]
    assert ["account_added", "lea", "", "boss"] in [r[1:] for r in rows]
    exported = json.loads(client.get("/admin/events/export?format=json").text)
    assert exported["schema"] == 1
    assert {"kind": "account_added", "subject": "lea", "detail": "", "actor": "boss"}.items() <= next(
        e for e in exported["events"] if e["kind"] == "account_added"
    ).items()
