# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The bridge on an MQTT broker: its state, retained, and a few orders it takes.

Topics, under `<prefix>/<uid>` (the prefix is `lantern-bridge` unless the settings say otherwise):

- `state`: a JSON document, `schema` 1, retained, published every 30 seconds. A reader skips the keys it does not
  know and treats a newer `schema` as absent.
- `availability`: `online` while the bridge runs, `offline` (the last will, and what it publishes on a clean
  stop). Retained.
- `order/rescan`: any payload reads every source again now. Orders are not retained; one that arrives retained
  is ignored.

No path, no address, no device name is published: a source is its id and its kind.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import paho.mqtt.client as paho
from paho.mqtt.enums import CallbackAPIVersion

from lantern_bridge import __version__, health
from lantern_bridge.config import AerofoilSource, FolderSource, Mqtt, OwnfoilSource

if TYPE_CHECKING:
    from lantern_bridge.runtime import Runtime

log = logging.getLogger("lantern_bridge.mqtt")

SCHEMA = 1
EVERY = 30.0

ClientFactory = Callable[[str], Any]


def _client(client_id: str) -> Any:
    return paho.Client(CallbackAPIVersion.VERSION2, client_id=client_id)


def _kind(source: object) -> str:
    return {FolderSource: "folder", OwnfoilSource: "ownfoil", AerofoilSource: "aerofoil"}.get(type(source), "tinfoil")


def state(runtime: Runtime, now: float | None = None) -> dict[str, Any]:
    """The document published on `state`."""
    index, stats, traffic = runtime.index, runtime.stats, runtime.traffic
    counts = index.counts()
    apps, titles = runtime.projection.served()
    checks = health.run(runtime)
    updates = runtime.updates
    document = {
        "schema": SCHEMA,
        "ts": int(time.time() if now is None else now),
        "bridge": {"uid": runtime.config.uid, "name": runtime.config.name, "version": __version__},
        "status": health.overall(checks),
        "checks": [{"name": c.name, "status": c.status} for c in checks],
        "library": {"titles": titles, "apps": apps},
        "files": {"hashed": counts["hashed"], "pending": counts["pending"], "pending_bytes": index.pending_bytes()},
        "traffic": {
            "requests": traffic.totals.requests,
            "sent_bytes": traffic.totals.sent,
            "errors": traffic.totals.errors,
            "refused": traffic.totals.refused,
            "transfers": len(traffic.active),
        },
        "sources": [
            {"id": s.id, "kind": _kind(s), "up": bool(stats.sources.get(s.id) and stats.sources[s.id].reachable)}
            for s in runtime.config.sources
        ],
        "uptime": int((time.time() if now is None else now) - stats.started),
    }
    if updates is not None and updates.checked is not None:  # only when the settings allow the check
        document["update"] = {"available": updates.available, "latest": updates.latest}
    return document


class Publisher:
    def __init__(self, runtime: Runtime, mqtt: Mqtt, factory: ClientFactory = _client) -> None:
        self.runtime = runtime
        self.base = f"{mqtt.prefix}/{runtime.config.uid}"
        self.state_topic, self.availability = f"{self.base}/state", f"{self.base}/availability"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = factory(f"lantern-bridge-{runtime.config.uid[:8]}")
        if mqtt.user is not None and mqtt.password_env is not None:
            self._client.username_pw_set(mqtt.user, os.environ.get(mqtt.password_env, ""))
        if mqtt.tls:
            self._client.tls_set()
        self._client.will_set(self.availability, "offline", qos=1, retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._mqtt = mqtt

    def start(self) -> None:
        self._client.connect_async(self._mqtt.host, self._mqtt.port)
        self._client.loop_start()
        self._thread = threading.Thread(target=self._loop, name="lantern-bridge-mqtt", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with contextlib.suppress(RuntimeError, ValueError):  # never connected
            self._client.publish(self.availability, "offline", qos=1, retain=True).wait_for_publish(timeout=2)
        self._client.disconnect()
        self._client.loop_stop()

    def publish(self) -> None:
        payload = json.dumps(state(self.runtime), separators=(",", ":"))
        self._client.publish(self.state_topic, payload, qos=1, retain=True)

    def _loop(self) -> None:
        while not self._stop.wait(EVERY):
            try:
                self.publish()
            except Exception:
                log.exception("the state could not be published")

    def _on_connect(self, client: Any, _userdata: Any, _flags: Any, reason: Any, *_: Any) -> None:
        if getattr(reason, "is_failure", False):
            log.warning("the MQTT broker refused the connection: %s", reason)
            return
        log.info("connected to the MQTT broker at %s:%s", self._mqtt.host, self._mqtt.port)
        client.publish(self.availability, "online", qos=1, retain=True)
        client.subscribe(f"{self.base}/order/+", qos=1)
        self.publish()

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        if getattr(message, "retain", False):
            return
        order = str(message.topic).rsplit("/", 1)[-1]
        if order == "rescan":
            log.info("rescan ordered over MQTT")
            self.runtime.rescan()
