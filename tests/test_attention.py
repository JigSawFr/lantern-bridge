"""The list at the top of the overview: what needs attention, most urgent first."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lantern_bridge.admin import attention
from lantern_bridge.admin.i18n import FR
from lantern_bridge.runtime import Runtime, SourceState
from test_admin import _admin, _client
from test_observer import SEE, _with_observer


def _runtime(client: TestClient) -> Runtime:
    runtime: Runtime = client.app.state.runtime  # type: ignore[attr-defined]
    return runtime


def test_a_quiet_bridge_has_nothing_to_deal_with(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    _runtime(client).cycle(batch=1)
    assert attention.collect(_runtime(client), 0, False, paired=None) == []
    _runtime(client).config.public = True  # nobody needs to be paired
    assert attention.collect(_runtime(client), 0, False, paired=0) == []
    assert "Needs your attention" not in client.get("/admin/").text


def test_the_most_urgent_comes_first_and_a_source_has_its_button(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    runtime = _runtime(client)
    runtime.cycle(batch=1)
    runtime.stats.sources["games"] = SourceState(reachable=False, error="gone")
    items = attention.collect(runtime, 2, True)
    assert [i.level for i in items] == sorted((i.level for i in items), key=attention._ORDER.get)  # type: ignore[arg-type]
    assert items[0].level == attention.BAD and items[0].scan == "games" and "gone" in items[0].detail
    page = client.get("/admin/").text
    assert "Needs your attention" in page and "A source does not answer" in page
    assert re.search(r'name="source" value="games"', page) and "Read again" in page
    texts = {i.text for i in items}
    assert "Devices are waiting to be paired" in texts and any("restart the bridge" in t for t in texts)


def test_every_kind_of_check_has_its_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _admin(_client(tmp_path, sources=False))
    runtime = _runtime(client)
    assert attention.collect(runtime, 0, False)[0].text == "No source configured yet."
    client = _admin(_client(tmp_path / "other", sources=True))
    runtime = _runtime(client)
    runtime.cycle(batch=1)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(100, 100, 1 << 20))
    runtime.stats.sources["games"].scanned = 1.0
    texts = {i.text for i in attention.collect(runtime, 0, False)}
    assert {"The disk of the bridge is nearly full", "A source has not been read for a while"} <= texts


def test_a_check_the_page_does_not_know_still_shows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lantern_bridge import health

    client = _admin(_client(tmp_path, sources=True))
    runtime = _runtime(client)
    monkeypatch.setattr(
        health,
        "run",
        lambda _r: [
            health.Check("odd", health.WARN, "hm"),
            health.Check("index", health.FAIL, "bad"),
            health.Check("atlas", health.WARN, "old"),
        ],
    )
    texts = [i.text for i in attention.collect(runtime, 0, False)]
    assert texts[0] == "The index of hashes is broken" and {
        "A check needs a look",
        "Atlas data is missing or old",
    } <= set(texts)


def test_every_text_has_its_french() -> None:
    source = Path(attention.__file__).read_text(encoding="utf-8")
    texts = re.findall(r'Item\((?:BAD, |WAIT, |INFO, |level, )"([^"]+)"', source)
    links = re.findall(r'"(/admin/[a-z]+)", "([^"]+)"', source)
    assert texts and all(t in FR for t in texts), [t for t in texts if t not in FR]
    assert all(link in FR for _, link in links)


def test_the_observer_sees_the_list_but_not_the_buttons(tmp_path: Path) -> None:
    boss, eye = _with_observer(tmp_path)
    _runtime(boss).cycle(batch=1)
    _runtime(boss).stats.sources["games"] = SourceState(reachable=False, error="gone")
    page = eye.get("/admin/").text
    assert "A source does not answer" in page and "Read again" not in page and SEE not in page
    assert "Read again" in boss.get("/admin/").text


def test_the_overview_blocks_come_alone_to_follow_the_server(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    runtime = _runtime(client)
    runtime.cycle(batch=1)
    runtime.config.public = True  # nobody needs to be paired
    page = client.get("/admin/").text
    assert 'data-live="/admin/?fragment=todo"' in page and 'data-live="/admin/?fragment=sources"' in page
    quiet = client.get("/admin/?fragment=todo")
    assert quiet.status_code == 200 and "<html" not in quiet.text and "Needs your attention" not in quiet.text
    runtime.stats.sources["games"] = SourceState(reachable=False, error="gone")
    todo = client.get("/admin/?fragment=todo").text
    assert "A source does not answer" in todo and "<html" not in todo
    sources = client.get("/admin/?fragment=sources").text
    assert "gone" in sources and "<html" not in sources and "<table>" in sources
    assert "<html" in client.get("/admin/?fragment=nothing").text  # an unknown block is the whole page


def test_the_observer_may_follow_the_blocks_too(tmp_path: Path) -> None:
    boss, eye = _with_observer(tmp_path)
    _runtime(boss).cycle(batch=1)
    reply = eye.get("/admin/?fragment=sources")
    assert reply.status_code == 200 and "<table>" in reply.text and "Rehash" in reply.text
