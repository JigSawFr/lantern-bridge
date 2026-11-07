# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""What goes through the server, for the administration pages: transfers, clients, rates, recent requests.

A pure ASGI middleware that watches every HTTP request and keeps counters in memory; the totals by day and
the machines seen are also written to ``activity.sqlite3`` (see history.py). Nothing leaves the process. It
never reads a body, never keeps a token and never changes an answer.
"""

from __future__ import annotations

import hashlib
import itertools
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from typing import Any

from lantern_bridge.history import Delta, History
from lantern_bridge.pulse import Pulse

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]

#: The requests of the administration pages are not the library's traffic.
_OWN = ("/admin", "/healthz", "/metrics")
#: What a browser, a scanner or a health check asks for: it moves no library data and says who asks nobody.
_SIMPLE = ("/", "/favicon.ico", "/robots.txt", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png")
WINDOW = 60  # seconds of history for the rates
FLUSH = 15.0  # seconds between two writes to the history
WHO_TTL = 60.0  # seconds a token's device is remembered (a revoked device is forgotten within a minute)


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
    key: str = ""  # who asked: "device:<id>" or "addr:<address>"
    label: str = ""  # the paired device's name and account, when the token is one

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
    simple: bool = False  # a simple request (see `Traffic.simple`): shown apart, never counted


@dataclass(slots=True)
class Client:
    """A machine that called the library: a paired device when its token says so, else its address."""

    key: str
    address: str
    agent: str
    first: float
    last: float
    requests: int = 0
    sent: int = 0
    label: str = ""

    @property
    def name(self) -> str:
        return self.label or self.address


@dataclass(slots=True)
class Totals:
    requests: int = 0
    sent: int = 0
    errors: int = 0  # 5xx
    refused: int = 0  # 401, 403
    simple: int = 0  # health checks, browsers and scanners: not the library's traffic, counted apart


@dataclass(slots=True)
class Traffic:
    keep: int = 300
    totals: Totals = field(default_factory=Totals)
    active: dict[int, Transfer] = field(default_factory=dict)
    recent: deque[Done] = field(default_factory=deque)
    clients: dict[str, Client] = field(default_factory=dict)
    #: Where the days and the machines are kept across restarts (None: memory only).
    history: History | None = None
    #: Maps a bearer token to (device id, label) for a paired device; set by the application.
    identify: Callable[[str], tuple[str, str] | None] | None = None
    #: Bumped when a transfer starts or ends, so the pages' live blocks look again at once.
    pulse: Pulse | None = None
    _pending: dict[str, list[Any]] = field(default_factory=dict)
    _who: dict[str, tuple[float, tuple[str, str] | None]] = field(default_factory=dict)
    _flushed: float = field(default_factory=time.monotonic)
    _buckets: dict[int, list[int]] = field(default_factory=dict)  # second -> [requests, bytes]
    _ids: Any = field(default_factory=itertools.count)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.recent = deque(maxlen=self.keep)
        if self.history is not None:
            for m in self.history.machines():
                self.clients[m.key] = Client(m.key, m.address, m.agent, m.first, m.last, m.requests, m.sent, m.label)

    def who(self, address: str, authorization: str | None) -> tuple[str, str]:
        """(key, label) of the machine: the paired device behind a bearer token, else its address."""
        parts = (authorization or "").split(None, 1)
        if self.identify is not None and len(parts) == 2 and parts[0].lower() == "bearer":
            digest = hashlib.sha256(parts[1].encode()).hexdigest()  # the token itself is never kept
            now = time.monotonic()
            with self._lock:
                cached = self._who.get(digest)
            if cached is None or cached[0] < now:
                found = self.identify(parts[1])
                with self._lock:
                    if len(self._who) > 256:
                        self._who.clear()
                    self._who[digest] = cached = (now + WHO_TTL, found)
            if cached[1] is not None:
                return f"device:{cached[1][0]}", cached[1][1]
        return f"addr:{address}", ""

    # -- recording -------------------------------------------------------------------------------

    @staticmethod
    def simple(path: str, authorization: str | None) -> bool:
        """Whether a request is noise: the discovery document (the container's health check asks for it every
        thirty seconds), the root page and a favicon (a browser), a scanner's guesses. A request with a token is
        always the library's, whatever it asks for; so is any other path, from wherever it comes (behind a
        proxy on the same machine, everybody is 127.0.0.1)."""
        if authorization:
            return False
        return path in _SIMPLE or path.startswith("/.well-known/")

    def note_simple(self, client: str, method: str, path: str, status: int, agent: str) -> None:
        """A simple request is over: counted apart and kept in the recent list, flagged, for the developer page."""
        with self._lock:
            self.totals.simple += 1
            self.recent.append(Done(time.time(), client, method, path, status, 0.0, 0, agent, simple=True))
        if self.pulse is not None:
            self.pulse.bump()

    def begin(
        self, client: str, method: str, path: str, range_: str | None, authorization: str | None = None
    ) -> Transfer:
        key, label = self.who(client, authorization)
        transfer = Transfer(next(self._ids), client, method, path, time.monotonic(), range=range_, key=key, label=label)
        with self._lock:
            self.active[transfer.id] = transfer
        if self.pulse is not None:
            self.pulse.bump()
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
            seen = self.clients.get(transfer.key)
            if seen is None:
                if len(self.clients) >= 200:  # a scan of addresses must not grow the table without end
                    oldest = min(self.clients.values(), key=lambda c: c.last)
                    del self.clients[oldest.key]
                seen = self.clients[transfer.key] = Client(transfer.key, transfer.client, agent, now, now)
            seen.agent, seen.last, seen.address = agent or seen.agent, now, transfer.client
            seen.label = transfer.label or seen.label
            seen.requests += 1
            seen.sent += transfer.sent
            if self.history is not None:
                pend = self._pending.setdefault(
                    transfer.key, [transfer.label, transfer.client, agent, now, now, 0, 0, 0]
                )
                pend[0], pend[1], pend[2], pend[4] = transfer.label, transfer.client, agent or pend[2], now
                pend[5] += 1
                pend[6] += transfer.sent
                pend[7] += 1 if transfer.status >= 500 else 0
        self.flush()
        if self.pulse is not None:
            self.pulse.bump()

    def flush(self, *, force: bool = False) -> None:
        """Writes what happened since the last write (at most every `FLUSH` seconds, or when forced)."""
        if self.history is None:
            return
        with self._lock:
            if not self._pending or (not force and time.monotonic() - self._flushed < FLUSH):
                return
            pending, self._pending = self._pending, {}
            self._flushed = time.monotonic()
        self.history.add(Delta(key, *values) for key, values in pending.items())

    def close(self) -> None:
        self.flush(force=True)

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
        if self.traffic.simple(path, headers.get("authorization")):
            status = 0

            async def watching(message: Message) -> None:
                nonlocal status
                if message["type"] == "http.response.start":
                    status = int(message.get("status", 0))
                await send(message)

            try:
                await self.app(scope, receive, watching)
            finally:
                self.traffic.note_simple(
                    client, scope.get("method", "GET"), path, status, headers.get("user-agent", "")[:120]
                )
            return
        transfer = self.traffic.begin(
            client, scope.get("method", "GET"), path, headers.get("range"), headers.get("authorization")
        )

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
