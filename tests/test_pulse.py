# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The live blocks wait for a change instead of asking every second."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from lantern_bridge.pulse import Pulse
from test_activity import _public
from test_admin import _admin


def test_a_bump_from_another_thread_wakes_a_waiter() -> None:
    pulse = Pulse()

    async def run() -> float:
        started = time.monotonic()
        with pulse.listen() as changed:
            threading.Timer(0.1, pulse.bump).start()
            await asyncio.wait_for(changed.wait(), 3)
        return time.monotonic() - started

    assert run_it(run()) < 1.0
    assert pulse.listeners == 0


def run_it[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


def test_a_change_while_the_page_is_rendered_is_not_lost() -> None:
    pulse = Pulse()

    async def run() -> bool:
        with pulse.listen() as changed:  # opened first, as the live stream does
            pulse.bump()  # the change happens while it renders
            await asyncio.sleep(0.05)
            return changed.is_set()

    assert run_it(run()) is True


def test_a_bump_with_nobody_waiting_or_a_closed_loop_is_harmless() -> None:
    pulse = Pulse()
    pulse.bump()
    loop = asyncio.new_event_loop()

    async def listen() -> None:
        with pulse.listen():
            loop.stop()  # leaves the waiter registered on a loop that is about to close

    loop.run_until_complete(listen())
    loop.close()
    pulse.bump()  # must not raise


def test_what_the_pages_show_bumps_the_pulse(tmp_path: Path) -> None:
    client = _admin(_public(tmp_path))
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    seen: list[int] = []
    original = runtime.pulse.bump

    def counting() -> None:
        seen.append(1)
        original()

    runtime.pulse.bump = counting
    runtime.history.event("settings", detail="saved")  # a journal line
    assert len(seen) == 1
    client.get("/api/lantern/v1/apps")  # a request starts and ends
    assert len(seen) >= 3
    before = len(seen)
    runtime.cycle(batch=5)  # a pass ends
    assert len(seen) > before
