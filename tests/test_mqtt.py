"""The bridge on an MQTT broker: its settings, the state it publishes, the orders it takes. No test opens a socket."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from lantern.uid import new_uid

from lantern_bridge.config import Config, ConfigError, FolderSource, Mqtt, load, parse_mqtt, save
from lantern_bridge.mqtt import SCHEMA, Publisher, state
from lantern_bridge.runtime import Runtime
from test_admin import _admin, _client

UID = "0c9c2a8e-5d3a-4c7e-8b0a-6f1d2e3c4b5a"


class FakeClient:
    """What the publisher asks of paho's client, recorded."""

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.published: list[tuple[str, str, bool]] = []
        self.subscribed: list[str] = []
        self.will: tuple[str, str, bool] | None = None
        self.login: tuple[str, str] | None = None
        self.tls = False
        self.started = self.stopped = self.disconnected = False
        self.on_connect: Any = None
        self.on_message: Any = None

    def username_pw_set(self, user: str, password: str) -> None:
        self.login = (user, password)

    def tls_set(self) -> None:
        self.tls = True

    def will_set(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
        self.will = (topic, payload, retain)

    def connect_async(self, host: str, port: int) -> None:
        self.address = (host, port)

    def loop_start(self) -> None:
        self.started = True

    def loop_stop(self) -> None:
        self.stopped = True

    def disconnect(self) -> None:
        self.disconnected = True

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribed.append(topic)

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> Any:
        self.published.append((topic, payload, retain))
        return SimpleNamespace(wait_for_publish=lambda timeout=None: None)


def _runtime(tmp_path: Path, mqtt: Mqtt | None = None) -> Runtime:
    games = tmp_path / "games"
    games.mkdir(exist_ok=True)
    config = Config(uid=UID, name="Shelf", mqtt=mqtt, sources=[FolderSource("games", games)])
    from lantern_bridge.upstream import BridgeLibrary

    return Runtime(config, tmp_path / "state", BridgeLibrary(base="/api/lantern"))


def _publisher(tmp_path: Path, **kw: Any) -> tuple[Publisher, FakeClient]:
    clients: list[FakeClient] = []

    def factory(client_id: str) -> FakeClient:
        clients.append(FakeClient(client_id))
        return clients[0]

    mqtt = Mqtt("broker.example.org", **kw)
    return Publisher(_runtime(tmp_path, mqtt), mqtt, factory), clients[0]


def test_the_settings_are_kept_in_the_file_with_what_this_version_does_not_know(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    save(path, Config(uid=UID, mqtt=Mqtt("broker.example.org", 8883, "bridge", "MQTT_PASSWORD", "shelf", True)))
    text = path.read_text()
    assert "[mqtt]" in text and "MQTT_PASSWORD" in text and "secret" not in text
    path.write_text(text.replace("[mqtt]", '[mqtt]\nfuture = "kept"'))
    again = load(path)
    assert again.mqtt == Mqtt("broker.example.org", 8883, "bridge", "MQTT_PASSWORD", "shelf", True, {"future": "kept"})
    save(path, again)
    assert 'future = "kept"' in path.read_text()
    save(path, Config(uid=UID))
    assert load(path).mqtt is None and "[mqtt]" not in path.read_text()


@pytest.mark.parametrize(
    "bad",
    [
        "x",
        {},
        {"host": ""},
        {"host": "h", "port": 0},
        {"host": "h", "port": True},
        {"host": "h", "prefix": "a/#"},
        {"host": "h", "prefix": "/a"},
        {"host": "h", "tls": "yes"},
        {"host": "h", "user": "u"},
        {"host": "h", "password_env": "P"},
    ],
)
def test_a_bad_mqtt_table_is_refused(bad: object) -> None:
    with pytest.raises(ConfigError):
        parse_mqtt(bad)


def test_the_state_is_a_versioned_document_without_paths_or_addresses(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    (tmp_path / "games" / "Aurora Quest [0100AAAA00000000][v0].nsp").write_bytes(b"base")
    runtime.cycle(batch=5)
    doc = state(runtime, now=1_000_000)
    assert doc["schema"] == SCHEMA == 1 and doc["ts"] == 1_000_000
    assert doc["bridge"] == {"uid": UID, "name": "Shelf", "version": doc["bridge"]["version"]}
    assert doc["status"] == "ok" and doc["files"]["hashed"] == 1 and doc["files"]["pending"] == 0
    (only,) = doc["sources"]
    assert {k: only[k] for k in ("id", "kind", "up")} == {"id": "games", "kind": "folder", "up": True}
    assert isinstance(only["free"], int)  # the room left on its volume
    assert set(doc["traffic"]) == {"requests", "sent_bytes", "errors", "refused", "transfers"}
    assert str(tmp_path) not in json.dumps(doc) and "Aurora" not in json.dumps(doc)


def test_the_publisher_announces_itself_and_publishes_the_state_retained(tmp_path: Path) -> None:
    publisher, client = _publisher(tmp_path, user="bridge", password_env="MQTT_PASSWORD", tls=True)
    assert client.client_id == f"lantern-bridge-{UID[:8]}" and client.tls and client.login == ("bridge", "")
    assert client.will == (f"lantern-bridge/{UID}/availability", "offline", True)
    publisher.start()
    assert client.started and client.address == ("broker.example.org", 1883)
    client.on_connect(client, None, None, SimpleNamespace(is_failure=False))
    topics = {t: (p, r) for t, p, r in client.published}
    assert topics[f"lantern-bridge/{UID}/availability"] == ("online", True)
    assert (
        topics[f"lantern-bridge/{UID}/state"][1] is True
        and json.loads(topics[f"lantern-bridge/{UID}/state"][0])["schema"] == 1
    )
    assert client.subscribed == [f"lantern-bridge/{UID}/order/+"]
    publisher.stop()
    assert client.published[-1] == (f"lantern-bridge/{UID}/availability", "offline", True) and client.disconnected


def test_a_refused_connection_publishes_nothing(tmp_path: Path) -> None:
    _, client = _publisher(tmp_path)
    client.on_connect(client, None, None, SimpleNamespace(is_failure=True))
    assert client.published == [] and client.subscribed == []


def test_a_rescan_order_reads_the_sources_again_and_a_retained_one_is_ignored(tmp_path: Path) -> None:
    publisher, client = _publisher(tmp_path)
    runtime = publisher.runtime
    runtime._shop_scanned["shop"] = 1.0
    order = SimpleNamespace(topic=f"lantern-bridge/{UID}/order/rescan", payload=b"", retain=True)
    client.on_message(client, None, order)
    assert runtime._shop_scanned and not runtime._wake.is_set()
    client.on_message(client, None, SimpleNamespace(topic=order.topic, payload=b"", retain=False))
    assert not runtime._shop_scanned and runtime._wake.is_set()
    client.on_message(
        client, None, SimpleNamespace(topic=f"lantern-bridge/{UID}/order/unknown", payload=b"", retain=False)
    )


def test_the_pages_save_the_broker_and_empty_host_turns_it_off(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    path = tmp_path / "state" / "config.toml"
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/admin/config").text).group(1)  # type: ignore[union-attr]
    form = {
        "csrf": csrf,
        "host": "broker.example.org",
        "port": "8883",
        "user": "bridge",
        "password_env": "MQTT_PASSWORD",
        "tls": "on",
    }
    assert "MQTT broker" in client.get("/admin/config").text
    assert client.post("/admin/config/mqtt", data=form).status_code == 303
    saved = load(path).mqtt
    assert saved == Mqtt("broker.example.org", 8883, "bridge", "MQTT_PASSWORD", "lantern-bridge", True)
    assert 'value="broker.example.org"' in client.get("/admin/config").text
    for bad in ({"port": "many"}, {"port": "0"}, {"password_env": "not a name"}, {"user": "", "password_env": "P"}):
        assert client.post("/admin/config/mqtt", data={**form, **bad}).status_code == 400, bad
    assert client.post("/admin/config/mqtt", data={"csrf": csrf, "host": ""}).status_code == 303
    assert load(path).mqtt is None
    assert new_uid()


def test_each_journal_line_is_published_as_it_happens_and_the_state_counts_the_problems(tmp_path: Path) -> None:
    publisher, client = _publisher(tmp_path)
    runtime = publisher.runtime
    assert publisher.event_topic == f"lantern-bridge/{UID}/event"
    runtime.history.event("source_down", subject="games", detail="gone")
    topic, payload, retained = client.published[-1]
    assert topic == publisher.event_topic and retained is False
    assert json.loads(payload) == {
        "schema": SCHEMA,
        "ts": json.loads(payload)["ts"],
        "kind": "source_down",
        "subject": "games",
        "detail": "gone",
    }
    assert state(runtime)["problems"] >= 0

    def boom(_event: Any) -> None:
        raise RuntimeError("listener")

    runtime.history.listener = boom
    runtime.history.event("scan")  # a failing listener never reaches the caller
    assert runtime.history.events(1)[0].kind == "scan"
