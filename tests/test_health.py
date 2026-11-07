"""The health checks, `/healthz` and the overview's Health block."""

from __future__ import annotations

import shutil
from pathlib import Path

from lantern_bridge import __version__, health
from lantern_bridge.runtime import SourceState
from test_admin import _admin, _client


def test_healthz_gives_the_status_and_to_the_local_network_who_answers(tmp_path: Path) -> None:
    client = _client(tmp_path, sources=True)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    first = client.get("/healthz")
    assert first.status_code == 200 and first.json()["status"] == "warn"  # the source is not read yet
    runtime.cycle(batch=1)
    body = client.get("/healthz").json()
    assert body == {"status": "ok", "version": __version__, "uid": runtime.config.uid}
    far = _client(tmp_path / "far", lan_only=False, host="8.8.8.8")  # not the local network: the status only
    assert set(far.get("/healthz").json()) == {"status"}
    assert runtime.traffic.totals.requests == 0  # a probe is not the library's traffic


def test_a_source_that_fails_makes_the_bridge_unhealthy(tmp_path: Path) -> None:
    client = _client(tmp_path, sources=True)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    runtime.stats.sources["games"] = SourceState(reachable=False, error="the volume is gone")
    answer = client.get("/healthz")
    assert answer.status_code == 503 and answer.json()["status"] == "fail"
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
