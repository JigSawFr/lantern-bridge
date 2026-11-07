# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""Something changed: the administration pages' live blocks look again, at once, instead of every second.

The runtime's threads and the traffic middleware call ``bump()`` when a pass ends, a file is hashed, a journal
line is written or a request starts or ends. A live stream waits on it (``listen`` before it renders, so a change
that happens while it renders is not lost) with a timeout, which is how the things that change with time alone,
the rates and the durations, still refresh while something is going on.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Iterator


class Pulse:
    """A change notifier that any thread may bump and any number of event loops may wait on."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    def bump(self) -> None:
        with self._lock:
            waiters = list(self._waiters)
        for loop, event in waiters:
            with contextlib.suppress(RuntimeError):  # the loop is closed: its waiter is going away
                loop.call_soon_threadsafe(event.set)

    @contextlib.contextmanager
    def listen(self) -> Iterator[asyncio.Event]:
        """An event that is set by the next bump. Open it before looking at the state, then wait on it."""
        entry = (asyncio.get_running_loop(), asyncio.Event())
        with self._lock:
            self._waiters.add(entry)
        try:
            yield entry[1]
        finally:
            with self._lock:
                self._waiters.discard(entry)

    @property
    def listeners(self) -> int:
        with self._lock:
            return len(self._waiters)
