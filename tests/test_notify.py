"""The short text sent to the administrator's address when a source stops, is back, or a release is out."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from lantern_bridge.config import Config, ConfigError, load, parse_webhook, save
from lantern_bridge.history import Event
from lantern_bridge.notify import Notifier
from test_admin import _admin, _client

URL = "https://ntfy.example.org/shelf"
UID = "0c9c2a8e-5d3a-4c7e-8b0a-6f1d2e3c4b5a"


def _notifier(answer: int = 200) -> tuple[Notifier, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(answer)

    return Notifier(URL, "Shelf", httpx.Client(transport=httpx.MockTransport(handler))), seen


def test_a_source_going_down_is_sent_as_an_urgent_text() -> None:
    notifier, seen = _notifier()
    notifier.on_event(Event(1.0, "source_down", "games", "No such file or directory"))
    (request,) = seen
    assert request.url == URL and request.headers["Title"] == "Source down" and request.headers["Priority"] == "high"
    assert request.read().decode() == "Shelf: source down - games - No such file or directory"
    assert notifier.error is None


def test_only_what_needs_a_person_is_sent() -> None:
    notifier, seen = _notifier()
    for kind in ("scan", "settings", "paired", "revoked", "started", "restored", "pairing_refused"):
        notifier.on_event(Event(1.0, kind, "x", "y"))
    assert seen == []
    for kind in ("source_up", "update"):
        notifier.on_event(Event(1.0, kind, "", "v1.2.0"))
    assert len(seen) == 2


@pytest.mark.parametrize("answer", [500, 403])
def test_a_refused_call_is_remembered_and_never_raises(answer: int) -> None:
    notifier, seen = _notifier(answer)
    notifier.on_event(Event(1.0, "source_down", "games", "gone"))
    assert notifier.error == str(answer) and len(seen) == 1


def test_a_network_error_is_remembered_by_its_kind() -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    notifier = Notifier(URL, "Shelf", httpx.Client(transport=httpx.MockTransport(boom)))
    notifier.on_event(Event(1.0, "source_down", "games", "gone"))
    assert notifier.error == "ConnectError"


def test_the_address_is_a_setting_and_is_checked(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    save(path, Config(uid=UID, notify=URL))
    assert f'notify = "{URL}"' in path.read_text() and load(path).notify == URL
    save(path, Config(uid=UID))
    assert "notify" not in path.read_text() and load(path).notify is None
    with pytest.raises(ConfigError, match="`notify`"):
        parse_webhook("ftp://nope", "notify")
    with pytest.raises(ConfigError, match="`notify`"):
        parse_webhook("https://user:pw@example.org/", "notify")


def test_the_settings_page_sets_it_and_the_runtime_listens(tmp_path: Path) -> None:
    import re

    client = _admin(_client(tmp_path, sources=True))
    page = client.get("/admin/config")
    assert 'name="notify"' in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
    form = {"csrf": csrf, "name": "Shelf", "remote_rate": "10", "admin_lan_only": "on", "low_disk_mb": "500"}
    assert client.post("/admin/config/settings", data={**form, "notify": URL}).status_code == 303
    assert load(tmp_path / "state" / "config.toml").notify == URL
    assert client.post("/admin/config/settings", data={**form, "notify": "ftp://nope"}).status_code == 400
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    assert runtime.notifier is None  # taken at the next start, like the other settings


def test_a_runtime_with_an_address_sends_what_its_journal_says(tmp_path: Path) -> None:
    from lantern_bridge.runtime import Runtime
    from lantern_bridge.upstream import BridgeLibrary

    runtime = Runtime(Config(uid=UID, name="Shelf", notify=URL), tmp_path / "state", BridgeLibrary(base="/api/lantern"))
    try:
        notifier, seen = _notifier()
        runtime.history.listeners[:] = [notifier.on_event]  # the real client would call the network
        runtime.history.event("source_down", "games", "gone")
        assert len(seen) == 1 and runtime.notifier is not None
    finally:
        runtime.stop()
