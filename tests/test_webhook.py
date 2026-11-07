"""The call to an address when scans found changes: gathered, small, and never a path."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx
import pytest
from lantern.server.file_index import SyncResult

from lantern_bridge.config import Config, ConfigError, load, parse_webhook, save
from lantern_bridge.webhook import SCHEMA, Webhook
from test_admin import _admin, _client

UID = "0c9c2a8e-5d3a-4c7e-8b0a-6f1d2e3c4b5a"
URL = "https://hooks.example.org/lantern/secret-id"


def _hook(status: int = 200) -> tuple[Webhook, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status)

    return Webhook(URL, UID, "Shelf", httpx.Client(transport=httpx.MockTransport(answer)), gather=3600), seen


def test_changes_are_gathered_into_one_call_without_paths() -> None:
    hook, seen = _hook()
    hook.note("games", SyncResult(2, 0, 0, added_paths=("a.nsp", "b.nsp")))
    hook.note("games", SyncResult(1, 1, 0, added_paths=("c.nsp",), changed_paths=("d.nsp",)))
    hook.note("shop", SyncResult(0, 0, 3, removed_paths=("x",)))
    hook.note("shop", SyncResult(0, 0, 0))  # nothing found: nothing noted
    assert seen == [] and hook.flush() and len(seen) == 1
    body = json.loads(seen[0].content)
    assert body["schema"] == SCHEMA == 1 and body["event"] == "scan"
    assert body["bridge"]["uid"] == UID and body["bridge"]["name"] == "Shelf"
    assert body["sources"] == {
        "games": {"added": 3, "changed": 1, "removed": 0},
        "shop": {"added": 0, "changed": 0, "removed": 3},
    }
    assert ".nsp" not in seen[0].content.decode()
    assert hook.flush() and len(seen) == 1  # nothing pending: no second call
    hook.close()


def test_a_failed_call_is_logged_and_dropped() -> None:
    hook, seen = _hook(status=500)
    hook.note("games", SyncResult(1, 0, 0))
    assert not hook.flush() and hook.error == "500"
    assert hook.flush() and len(seen) == 1  # not repeated
    hook.close()


def test_an_unreachable_receiver_does_not_raise() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    hook = Webhook(URL, UID, "Shelf", httpx.Client(transport=httpx.MockTransport(refuse)), gather=3600)
    hook.note("games", SyncResult(1, 0, 0))
    assert not hook.flush() and hook.error == "ConnectError"
    hook.close()


def test_the_timer_sends_after_the_gathering_time() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(answer))
    hook = Webhook(URL, UID, "Shelf", client, gather=0.05)
    hook.note("games", SyncResult(1, 0, 0))
    for _ in range(100):
        if seen:
            break
        time.sleep(0.02)
    assert len(seen) == 1
    hook.close()


@pytest.mark.parametrize("bad", [5, "ftp://x.example.org/", "http://", "https://user:pw@x.example.org/hook"])
def test_a_bad_address_is_refused(bad: object) -> None:
    with pytest.raises(ConfigError):
        parse_webhook(bad)


def test_the_address_is_kept_in_the_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    save(path, Config(uid=UID, webhook=URL))
    assert f'webhook = "{URL}"' in path.read_text() and load(path).webhook == URL
    save(path, Config(uid=UID))
    assert "webhook" not in path.read_text() and load(path).webhook is None
    assert parse_webhook("") is None and parse_webhook(None) is None


def test_the_settings_page_saves_it(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    path = tmp_path / "state" / "config.toml"
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/admin/config").text).group(1)  # type: ignore[union-attr]
    form = {"csrf": csrf, "name": "Shelf", "remote_rate": "10", "admin_lan_only": "on"}
    assert client.post("/admin/config/settings", data={**form, "webhook": URL}).status_code == 303
    assert load(path).webhook == URL and f'value="{URL}"' in client.get("/admin/config").text
    assert client.post("/admin/config/settings", data={**form, "webhook": "ftp://nope"}).status_code == 400
    assert client.post("/admin/config/settings", data={**form, "webhook": ""}).status_code == 303
    assert load(path).webhook is None


def test_a_scan_of_the_runtime_reaches_the_webhook(tmp_path: Path) -> None:
    client = _client(tmp_path, sources=True)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    hook, seen = _hook()
    runtime.webhook = hook
    (tmp_path / "games" / "Aurora Quest [0100AAAA00000000][v0].nsp").write_bytes(b"base")
    runtime.cycle(batch=5)
    assert hook.flush() and json.loads(seen[0].content)["sources"]["games"]["added"] == 1
