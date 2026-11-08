"""The journal's export and the restoration line."""

from __future__ import annotations

import csv
import io
import uuid
from pathlib import Path

from lantern_bridge import backup
from lantern_bridge.__main__ import main
from test_admin import _admin, _client


def test_the_journal_downloads_as_csv_and_json_and_a_formula_stays_text(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    history = client.app.state.runtime.history  # type: ignore[attr-defined]
    history.event("settings", subject="=SUM(A1)", detail="+cmd")
    history.event("paired", subject="alice", detail="ok")
    page = client.get("/admin/events")
    assert "/admin/events/export?format=csv" in page.text
    sheet = client.get("/admin/events/export?format=csv")
    assert sheet.status_code == 200 and "attachment" in sheet.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(sheet.text)))
    assert rows[0] == ["time", "kind", "subject", "detail", "actor"]
    assert ["settings", "'=SUM(A1)", "'+cmd", ""] in [r[1:] for r in rows]
    data = client.get("/admin/events/export?format=json&kind=paired").json()
    assert data["schema"] == 1 and [e["subject"] for e in data["events"]] == ["alice"]


def test_the_observer_cannot_download_the_journal(tmp_path: Path) -> None:
    from test_observer import _with_observer

    _, eye = _with_observer(tmp_path)
    assert eye.get("/admin/events/export").status_code == 403


def test_a_restoration_is_in_the_journal(tmp_path: Path) -> None:
    from lantern_bridge.config import Config, save
    from lantern_bridge.history import History

    state = tmp_path / "state"
    state.mkdir()
    config = state / "config.toml"
    save(config, Config(uid=str(uuid.uuid4())))
    archive = tmp_path / "b.zip"
    backup.make(state, config, archive)
    assert main(["restore", str(archive), "--config", str(config)]) == 0
    history = History(state / "activity.sqlite3")
    found = history.events(5, "restored")
    history.close()
    assert len(found) == 1 and "config.toml" in found[0].detail
