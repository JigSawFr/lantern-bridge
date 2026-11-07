# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The gain estimated before a conversion, the progress while it runs, and the dots of the menu."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lantern_bridge.compress import RATIOS, Compressor, prior_ratio, ratio_key
from test_admin import _admin, _client, _csrf
from test_compress import FAKE, _convert, _ready, _wait, tool  # noqa: F401  (the fixture)
from test_observer import _with_observer
from test_writable import UPDATE, _shelf


def test_a_typical_ratio_is_smaller_for_a_higher_level_and_bigger_for_a_block_or_a_way_back() -> None:
    levels = [prior_ratio("compress", "nsz", level, False) for level in (1, 3, 10, 18, 22)]
    assert levels == sorted(levels, reverse=True) and 0.6 < levels[-1] < levels[0] < 0.9
    assert prior_ratio("compress", "nsz", 18, True) > prior_ratio("compress", "nsz", 18, False)
    assert prior_ratio("compress", "xcz", 18, False) < prior_ratio("compress", "nsz", 18, False)
    assert prior_ratio("decompress", "nsp", 18, False) > 1.2  # the way back makes it bigger
    assert prior_ratio("compress", "nsz", 99, False) == prior_ratio("compress", "nsz", 22, False)  # clamped


def test_the_estimate_learns_from_the_conversions_done_here(tmp_path: Path) -> None:
    compressor = Compressor(tmp_path)
    expected, measured = compressor.estimate("nsp", 1000, 18, False)
    assert expected == int(1000 * prior_ratio("compress", "nsz", 18, False)) and measured is False
    for ratio in (0.50, 0.60, 0.55):
        compressor._remember(ratio_key("nsp", "nsz", 18, False), ratio)
    assert compressor.estimate("nsp", 1000, 18, False) == (550, True)  # the median of what was measured
    assert compressor.estimate("nsp", 1000, 22, False)[1] is False  # another level is another figure
    document = json.loads((tmp_path / RATIOS).read_text())
    assert document["schema"] == 1 and document["results"]["nsp>nsz:18:solid"] == [0.5, 0.6, 0.55]
    again = Compressor(tmp_path)  # a restart keeps them
    assert again.estimate("nsp", 1000, 18, False) == (550, True)


def test_the_ratios_file_is_read_with_care(tmp_path: Path) -> None:
    (tmp_path / RATIOS).write_text("this is not json")
    assert Compressor(tmp_path).estimate("nsp", 100)[1] is False  # absent or broken: typical figures
    newer = {"schema": 99, "results": {"nsp>nsz:18:solid": [0.5]}, "future": True}
    (tmp_path / RATIOS).write_text(json.dumps(newer))
    compressor = Compressor(tmp_path)
    assert compressor.estimate("nsp", 100)[1] is False  # a newer schema is read as absent...
    compressor._remember("nsp>nsz:18:solid", 0.4)
    assert json.loads((tmp_path / RATIOS).read_text()) == newer  # ...and never written over
    (tmp_path / RATIOS).write_text(
        json.dumps({"schema": 1, "results": {"nsp>nsz:18:solid": [0.5, "x", -2, 99]}, "extra": 1})
    )
    assert Compressor(tmp_path).estimate("nsp", 100) == (50, True)  # what makes no sense is dropped, the rest is kept


@pytest.mark.usefixtures("tool")
def test_the_convert_form_shows_the_expected_size_for_each_level_and_mode(tmp_path: Path) -> None:
    client, _ = _shelf(tmp_path)
    _ready(tmp_path)
    page = client.get("/admin/library/0100AAAA00000000").text
    table = json.loads(re.search(r"data-estimates='([^']*)'", page).group(1).replace("&#34;", '"'))  # type: ignore[union-attr]
    assert set(table) == {f"{lv}:{mode}" for lv in (3, 10, 18, 22) for mode in ("solid", "block")}
    assert table["18:solid"]["measured"] is False and "%" in table["18:solid"]["text"]
    assert "Estimated result" in page and "a typical figure" in page and "data-estimate-note" in page


@pytest.mark.usefixtures("tool")
def test_a_running_conversion_shows_how_far_it_is_and_how_long_is_left(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _shelf(tmp_path)
    _ready(tmp_path)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    monkeypatch.setenv("FAKE_NSZ", "progress")
    _convert(client, UPDATE)
    for _ in range(60):
        job = runtime.compressor.jobs()[0]
        if job.progress >= 0.49:
            break
        time.sleep(0.1)
    assert job.state == "running" and 0.49 <= job.progress < 0.99 and job.step == "Compressing"
    time.sleep(0.6)  # long enough for a first estimate of the time left
    assert job.eta(time.time()) is not None and job.speed(time.time()) > 0
    tasks = client.get("/admin/tasks?fragment=1").text
    assert "<progress" in tasks and "50 %" in tasks and "left" in tasks and "Compressing" in tasks
    assert (
        "Conversions" in client.get("/admin/activity").text and "50 %" in client.get("/admin/activity?fragment=1").text
    )
    finished = _wait(runtime.compressor)
    assert finished.progress == 1.0
    page = client.get("/admin/tasks").text
    assert "grew by" in page or "saved" in page  # the result, against the source
    assert "Conversions" not in client.get("/admin/activity").text  # nothing is running any more


@pytest.mark.usefixtures("tool")
def test_a_conversion_done_makes_the_next_estimate_a_measured_one(tmp_path: Path) -> None:
    client, _ = _shelf(tmp_path)
    _ready(tmp_path)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    _convert(client, UPDATE)
    _wait(runtime.compressor)
    assert runtime.compressor.estimate("nsp", 100)[1] is True
    assert "from your earlier conversions" in client.get("/admin/library/0100AAAA00000000").text


def _badges(client: TestClient) -> dict[str, dict[str, object]]:
    return client.get("/admin/api/badges").json()  # type: ignore[no-any-return]


@pytest.mark.usefixtures("tool")
def test_the_menu_marks_where_something_needs_a_look_or_is_going_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _shelf(tmp_path)
    _ready(tmp_path)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    assert _badges(client) == {}  # a quiet bridge shows no dot
    page = client.get("/admin/").text
    assert page.count("data-badge=") >= 6 and re.search(r'data-badge="tasks" hidden', page)  # in place, hidden
    client.post("/api/lantern/v1/pairing", json={"schema": 1, "device": "Den", "scopes": ["library"]})
    assert _badges(client)["pairings"] == {"n": 1, "level": "wait"}  # a device waits for you
    assert _badges(client)["manage"] == {"n": 1, "level": "wait"}  # the Manage button sums its pages
    monkeypatch.setenv("FAKE_NSZ", "slow")
    _convert(client, UPDATE)
    for _ in range(50):
        if runtime.compressor.jobs()[0].state == "running":
            break
        time.sleep(0.1)
    assert _badges(client)["tasks"] == {"n": 1, "level": "info"}  # something is going on
    csrf = _csrf(client.get("/admin/tasks").text)
    client.post(f"/admin/tasks/{runtime.compressor.jobs()[0].id}/cancel", data={"csrf": csrf})
    _wait(runtime.compressor, "cancelled")
    assert "tasks" not in _badges(client)
    monkeypatch.setenv("FAKE_NSZ", "fail")
    _convert(client, UPDATE)
    _wait(runtime.compressor, "failed")
    assert _badges(client)["tasks"] == {"n": 1, "level": "bad"}  # a failure is shown in red for an hour
    runtime.stats.sources["games"].reachable = False
    assert _badges(client)["sources"] == {"n": 1, "level": "bad"} and _badges(client)["overview"]["level"] == "bad"
    assert _badges(client)["manage"]["level"] == "bad"
    html = client.get("/admin/").text
    assert re.search(r'<span class="dot bad" data-badge="sources">1</span>', html)


def test_the_menu_dots_are_live_and_the_read_only_account_sees_only_its_own(tmp_path: Path) -> None:
    boss, eye = _with_observer(tmp_path)
    boss.post("/api/lantern/v1/pairing", json={"schema": 1, "device": "Den", "scopes": ["library"]})
    boss.app.state.runtime.stats.sources["games"].reachable = False  # type: ignore[attr-defined]
    assert set(_badges(boss)) >= {"pairings", "sources", "manage"}
    assert set(_badges(eye)) <= {"overview", "activity", "library"}  # no word of the pages it cannot open
    assert _client(tmp_path / "x").get("/admin/api/badges").status_code == 401


def test_the_dots_script_follows_the_badges_address(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    script = client.get("/admin/static/app.js").text
    assert "/admin/api/badges" in script and "data-badge" in script
    assert FAKE  # the stand-in is shared with the compression tests
