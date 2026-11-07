# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""What goes through the server, for the administration pages: transfers, clients, rates, recent requests.

A pure ASGI middleware that watches every HTTP request and keeps counters in memory (nothing is written
anywhere, nothing leaves the process). It never reads a body and never changes an answer.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]

#: The requests of the administration pages are not the library's traffic.
_OWN = "/admin"
WINDOW = 60  # seconds of history for the rates


@dataclass(slots=True)
class Transfer:
    """A response being sent."""

    id: int
    client: str
    method: str
    path: str
    started: float
    sent: int = 0
    status: int = 0
    range: str | None = None

    @property
    def rate(self) -> float:
        elapsed = time.monotonic() - self.started
        return self.sent / elapsed if elapsed > 0 else 0.0


@dataclass(frozen=True, slots=True)
class Done:
    """A request that is over."""

    time: float
    client: str
    method: str
    path: str
    status: int
    seconds: float
    sent: int
    agent: str


@dataclass(slots=True)
class Client:
    """A machine that called the library: by address, and the program it said it was."""

    address: str
    agent: str
    first: float
    last: float
    requests: int = 0
    sent: int = 0


@dataclass(slots=True)
class Totals:
    requests: int = 0
    sent: int = 0
    errors: int = 0  # 5xx
    refused: int = 0  # 401, 403


@dataclass(slots=True)
class Traffic:
    keep: int = 300
    totals: Totals = field(default_factory=Totals)
    active: dict[int, Transfer] = field(default_factory=dict)
    recent: deque[Done] = field(default_factory=deque)
    clients: dict[str, Client] = field(default_factory=dict)
    _buckets: dict[int, list[int]] = field(default_factory=dict)  # second -> [requests, bytes]
    _ids: Any = field(default_factory=itertools.count)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.recent = deque(maxlen=self.keep)

    # -- recording -------------------------------------------------------------------------------

    def begin(self, client: str, method: str, path: str, range_: str | None) -> Transfer:
        transfer = Transfer(next(self._ids), client, method, path, time.monotonic(), range=range_)
        with self._lock:
            self.active[transfer.id] = transfer
        return transfer

    def sent(self, transfer: Transfer, count: int) -> None:
        transfer.sent += count
        with self._lock:
            self._bucket()[1] += count

    def _bucket(self) -> list[int]:
        now = int(time.time())
        for old in [s for s in self._buckets if s < now - WINDOW]:
            del self._buckets[old]
        return self._buckets.setdefault(now, [0, 0])

    def end(self, transfer: Transfer, agent: str) -> None:
        now = time.time()
        with self._lock:
            self.active.pop(transfer.id, None)
            self._bucket()[0] += 1
            self.totals.requests += 1
            self.totals.sent += transfer.sent
            if transfer.status >= 500:
                self.totals.errors += 1
            if transfer.status in (401, 403):
                self.totals.refused += 1
            self.recent.append(
                Done(
                    now,
                    transfer.client,
                    transfer.method,
                    transfer.path,
                    transfer.status,
                    time.monotonic() - transfer.started,
                    transfer.sent,
                    agent,
                )
            )
            seen = self.clients.get(transfer.client)
            if seen is None:
                if len(self.clients) >= 200:  # a scan of addresses must not grow the table without end
                    oldest = min(self.clients.values(), key=lambda c: c.last)
                    del self.clients[oldest.address]
                seen = self.clients[transfer.client] = Client(transfer.client, agent, now, now)
            seen.agent, seen.last = agent or seen.agent, now
            seen.requests += 1
            seen.sent += transfer.sent

    # -- reading ---------------------------------------------------------------------------------

    def rates(self) -> dict[str, float]:
        """Requests and bytes a second over the last ten seconds."""
        now = int(time.time())
        with self._lock:
            window = [b for s, b in self._buckets.items() if now - 10 <= s <= now]
        return {"requests": sum(b[0] for b in window) / 10, "bytes": sum(b[1] for b in window) / 10}

    def snapshot(self) -> tuple[list[Transfer], list[Client], list[Done]]:
        with self._lock:
            transfers = sorted(self.active.values(), key=lambda t: t.started)
            clients = sorted(self.clients.values(), key=lambda c: -c.last)
            recent = list(reversed(self.recent))
        return transfers, clients, recent


class TrafficMiddleware:
    """Counts what `app` answers (HTTP only; a WebSocket or a lifespan passes through)."""

    def __init__(self, app: App, traffic: Traffic) -> None:
        self.app, self.traffic = app, traffic

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or path.startswith(_OWN):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        client = (scope.get("client") or ("-", 0))[0]
        transfer = self.traffic.begin(client, scope.get("method", "GET"), path, headers.get("range"))

        async def counting(message: Message) -> None:
            if message["type"] == "http.response.start":
                transfer.status = int(message.get("status", 0))
            elif message["type"] == "http.response.body":
                self.traffic.sent(transfer, len(message.get("body", b"")))
            await send(message)

        try:
            await self.app(scope, receive, counting)
        finally:
            self.traffic.end(transfer, headers.get("user-agent", "")[:120])
