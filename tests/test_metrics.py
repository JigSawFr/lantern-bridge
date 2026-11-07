"""The /metrics page: off by default, local only, the Prometheus text format."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from lantern.uid import new_uid

from lantern_bridge.app import make_app
from lantern_bridge.config import Config, FolderSource, load, save


def _metrics(tmp_path: Path, *, on: bool = True, host: str = "127.0.0.1", **more: Any) -> TestClient:
    games = tmp_path / "games"
    games.mkdir(exist_ok=True)
    config = Config(uid=new_uid(), name="Shelf", metrics=on, sources=[FolderSource("games", games)], **more)
    return TestClient(make_app(config, tmp_path / "state", run=False), client=(host, 50000))


def test_metrics_is_off_unless_asked_for(tmp_path: Path) -> None:
    assert _metrics(tmp_path, on=False).get("/metrics").status_code == 404
    config = tmp_path / "config.toml"
    save(config, Config(uid=new_uid(), metrics=True))
    assert "metrics = true" in config.read_text() and load(config).metrics is True


def test_metrics_answers_the_local_network_only(tmp_path: Path) -> None:
    assert _metrics(tmp_path, host="8.8.8.8").get("/metrics").status_code == 403
    assert _metrics(tmp_path, host="192.168.1.20").get("/metrics").status_code == 200


def test_metrics_follows_the_trusted_proxy_and_the_allowed_networks(tmp_path: Path) -> None:
    behind = _metrics(tmp_path, host="172.18.0.2", trusted_proxies=["172.18.0.2"])
    assert behind.get("/metrics", headers={"x-forwarded-for": "8.8.8.8"}).status_code == 403
    assert behind.get("/metrics", headers={"x-forwarded-for": "192.168.1.20"}).status_code == 200
    assert (
        _metrics(tmp_path, host="172.18.0.2").get("/metrics", headers={"x-forwarded-for": "8.8.8.8"}).status_code == 200
    )
    narrow = _metrics(tmp_path, host="192.168.1.20", admin_allow=["10.0.0.0/8"])
    assert narrow.get("/metrics").status_code == 403


def test_metrics_gives_the_figures_without_names_or_paths(tmp_path: Path) -> None:
    client = _metrics(tmp_path)
    (tmp_path / "games" / "Aurora Quest [0100AAAA00000000][v0].nsp").write_bytes(b"base")
    client.app.state.runtime.cycle(batch=5)  # type: ignore[attr-defined]
    client.get("/.well-known/lantern")
    reply = client.get("/metrics")
    assert reply.headers["content-type"].startswith("text/plain; version=0.0.4")
    text = reply.text
    assert 'lantern_bridge_files{state="hashed"} 1' in text
    assert 'lantern_bridge_source_up{source="games"} 1' in text
    assert "lantern_bridge_requests_total 1" in text  # the metrics request itself is not counted
    assert 'lantern_bridge_health{status="ok"} 1' in text
    assert "Aurora" not in text and "games/" not in text and "127.0.0.1" not in text
    assert text.endswith("\n") and "# TYPE lantern_bridge_requests_total counter" in text


def test_a_pass_without_scan_leaves_the_sources_alone(tmp_path: Path) -> None:
    client = _metrics(tmp_path)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    runtime.cycle(batch=5)
    (tmp_path / "games" / "Aurora Quest [0100AAAA00000000][v0].nsp").write_bytes(b"base")
    assert runtime.cycle(batch=5, scan=False) == 0  # the new file is not read yet
    assert runtime.cycle(batch=5) == 1
