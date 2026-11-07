# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""A call to an address of the administrator's when scans found changes.

One POST carries what ten seconds of scans found, so a big import is one call, not hundreds. The body is JSON,
`schema` 1: the bridge (id, name, version), the time, and per source how many files were added, changed or
removed. No path and no title: a receiver that wants them reads the bridge. A call that fails is logged and
dropped; nothing is queued for later.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

import httpx

from lantern_bridge import __version__

if TYPE_CHECKING:
    from lantern_bridge.index import SyncResult

log = logging.getLogger("lantern_bridge.webhook")

SCHEMA = 1
#: Seconds the changes of several scans are gathered before the call.
GATHER = 10.0
TIMEOUT = 5.0


class Webhook:
    def __init__(
        self, url: str, uid: str, name: str, client: httpx.Client | None = None, gather: float = GATHER
    ) -> None:
        self.url, self.uid, self.name = url, uid, name
        self._client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
        self._gather = gather
        self._lock = threading.Lock()
        self._pending: dict[str, dict[str, int]] = {}
        self._timer: threading.Timer | None = None
        self.error: str | None = None

    def note(self, source: str, result: SyncResult) -> None:
        """What a scan of `source` found; nothing is sent when it found nothing."""
        if not (result.added or result.changed or result.removed):
            return
        with self._lock:
            mine = self._pending.setdefault(source, {"added": 0, "changed": 0, "removed": 0})
            mine["added"] += result.added
            mine["changed"] += result.changed
            mine["removed"] += result.removed
            if self._timer is None:
                self._timer = threading.Timer(self._gather, self.flush)
                self._timer.daemon = True
                self._timer.start()

    def body(self, sources: dict[str, dict[str, int]]) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "event": "scan",
            "ts": int(time.time()),
            "bridge": {"uid": self.uid, "name": self.name, "version": __version__},
            "sources": sources,
        }

    def flush(self) -> bool:
        """Send what is pending now. True when the receiver answered 2xx."""
        with self._lock:
            sources, self._pending, self._timer = self._pending, {}, None
        if not sources:
            return True
        try:
            reply = self._client.post(self.url, json=self.body(sources))
            reply.raise_for_status()
        except httpx.HTTPError as error:
            self.error = (
                type(error).__name__
                if not isinstance(error, httpx.HTTPStatusError)
                else str(error.response.status_code)
            )
            log.warning("the webhook did not take the call: %s", self.error)
            return False
        self.error = None
        return True

    def close(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        self._client.close()
