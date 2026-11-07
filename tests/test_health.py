"""The health checks, `/healthz` and the overview's Health block."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from lantern_bridge import health
from lantern_bridge.runtime import SourceState
from test_admin import _admin, _client


def test_healthz_gives_the_whole_status_and_nothing_else(tmp_path: Path) -> None:
    client = _client(tmp_path, sources=True)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    first = client.get("/healthz")
    assert first.status_code == 200 and first.json() == {"status": "warn"}  # the source is not read yet
    runtime.cycle(batch=1)
    assert client.get("/healthz").json() == {"status": "ok"}
    assert runtime.traffic.totals.requests == 0  # a probe is not the library's traffic


def test_a_source_that_fails_makes_the_bridge_unhealthy(tmp_path: Path) -> None:
    client = _client(tmp_path, sources=True)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    runtime.stats.sources["games"] = SourceState(reachable=False, error="the volume is gone")
    answer = client.get("/healthz")
    assert answer.status_code == 503 and answer.json() == {"status": "fail"}
    assert "the volume is gone" in {c.name: c.detail for c in health.run(runtime)}["source:games"]


def test_old_scans_a_full_disk_and_an_old_atlas_are_warned_about(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = _client(tmp_path, sources=True)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    runtime.cycle(batch=1)
    later = {c.name: c for c in health.run(runtime, now=runtime.stats.sources["games"].scanned + 3600)}
    assert later["scan:games"].status == health.WARN
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(100, 100, 100 << 20))
    assert {c.name: c for c in health.run(runtime)}["disk"].status == health.WARN
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(100, 100, 1 << 20))
    assert {c.name: c for c in health.run(runtime)}["disk"].status == health.FAIL


def test_the_overview_says_so(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    client.app.state.runtime.cycle(batch=1)  # type: ignore[attr-defined]
    assert "all good" in client.get("/admin/").text
    client.app.state.runtime.stats.sources["games"] = SourceState(reachable=False, error="gone")  # type: ignore[attr-defined]
    assert "gone" in client.get("/admin/").text


def test_the_free_space_limit_is_a_setting_and_a_source_says_how_much_room_its_volume_has(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lantern_bridge.config import Config, ConfigError, load, save
    from lantern_bridge.mqtt import state

    client = _client(tmp_path, sources=True)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    runtime.cycle(batch=1)
    free = runtime.stats.sources["games"].free
    assert isinstance(free, int) and free > 0
    assert state(runtime)["sources"][0]["free"] == free
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(100, 100, 700 << 20))
    assert {c.name: c for c in health.run(runtime)}["disk"].status == health.OK  # 700 MiB, limit 500
    runtime.config.low_disk_mb = 1000
    assert {c.name: c for c in health.run(runtime)}["disk"].status == health.WARN
    path = tmp_path / "config.toml"
    save(path, Config(uid=runtime.config.uid, low_disk_mb=2000))
    assert "low_disk_mb = 2000" in path.read_text() and load(path).low_disk_mb == 2000
    for bad in ("low_disk_mb = 5", 'low_disk_mb = "many"', "low_disk_mb = true"):
        path.write_text(f'schema = 1\nuid = "{runtime.config.uid}"\n{bad}\n')
        try:
            load(path)
        except ConfigError:
            continue
        raise AssertionError(bad)


def test_the_overview_shows_the_room_and_the_settings_page_sets_the_limit(tmp_path: Path) -> None:
    import re

    from lantern_bridge.config import load

    client = _admin(_client(tmp_path, sources=True))
    client.app.state.runtime.cycle(batch=1)  # type: ignore[attr-defined]
    assert " free" in client.get("/admin/").text
    page = client.get("/admin/config")
    assert 'name="low_disk_mb"' in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
    form = {"csrf": csrf, "name": "Shelf", "remote_rate": "10", "admin_lan_only": "on"}
    assert client.post("/admin/config/settings", data={**form, "low_disk_mb": "900"}).status_code == 303
    assert load(tmp_path / "state" / "config.toml").low_disk_mb == 900
    for bad in ("many", "5"):
        assert client.post("/admin/config/settings", data={**form, "low_disk_mb": bad}).status_code == 400
