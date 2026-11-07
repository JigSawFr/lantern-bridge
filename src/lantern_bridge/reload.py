# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The server that can be swapped for a new one, so a change of settings needs no restart.

The application the web server runs is this shell. It holds one Lantern server (a FastAPI application with its
runtime and its accounts) and forwards every request to it. When the settings file changes (saved from the pages,
or edited by hand: the file is watched) the shell builds a new server from it and swaps:

1. the new server is built (it opens its stores; nothing runs yet);
2. the old runtime is halted: its passes, its broker and its notifications stop, its stores stay open;
3. the new runtime starts and takes the next requests;
4. the old server finishes the requests it is answering (a download included, up to ``DRAIN`` seconds), then closes.

The administrator's sessions and the recent log are shared, so nobody is signed out. A settings file that does not
load, or a server that cannot be built, leaves the old server as it is, and the pages say why. Only the address and
port the web server listens on need a restart: they are not in the file.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from lantern_bridge.config import Config, ConfigError, load

log = logging.getLogger("lantern_bridge.reload")

DRAIN = 30.0  # seconds an old server may take to finish the requests it is answering
WATCH = 3.0  # seconds between two looks at the settings file


class Reloadable:
    """An ASGI application: the Lantern server for the current settings, swappable."""

    def __init__(
        self, build: Callable[[Config, Reloadable], FastAPI], config: Config, path: Path, *, run: bool = True
    ) -> None:
        self._build = build
        self.path = path
        self.run = run
        self.current: FastAPI = build(config, self)
        self.error: str | None = None  # why the last reload did not apply, until one does
        self._busy: dict[int, int] = {}  # requests being answered, by server
        self._lock: asyncio.Lock | None = None
        self._seen = self._stamp()
        self._tasks: set[asyncio.Task[None]] = set()

    # -- the application ---------------------------------------------------------------------------

    @property
    def state(self) -> Any:
        """The current server's state: its runtime, its administration pages, its accounts."""
        return self.current.state

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        app = self.current
        key = id(app)
        self._busy[key] = self._busy.get(key, 0) + 1
        try:
            await app(scope, receive, send)
        finally:
            self._busy[key] -= 1

    async def _lifespan(self, receive: Any, send: Any) -> None:
        watcher: asyncio.Task[None] | None = None
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    if self.run:
                        self.current.state.runtime.start()
                        watcher = asyncio.create_task(self._watch())
                except Exception as error:  # noqa: BLE001  (the web server reports it and stops)
                    await send({"type": "lifespan.startup.failed", "message": str(error)})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                if watcher is not None:
                    watcher.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await watcher
                for task in list(self._tasks):
                    await task
                await asyncio.get_running_loop().run_in_executor(None, self._close, self.current)
                await send({"type": "lifespan.shutdown.complete"})
                return

    @staticmethod
    def _close(app: FastAPI) -> None:
        app.state.runtime.stop()
        app.state.accounts.close()

    # -- the settings file -------------------------------------------------------------------------

    def _stamp(self) -> tuple[int, int] | None:
        try:
            info = os.stat(self.path)
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    async def _watch(self) -> None:
        """Looks at the settings file now and then: a hand edit is applied like a save from the pages."""
        while True:
            await asyncio.sleep(WATCH)
            stamp = self._stamp()
            if stamp is not None and stamp != self._seen:
                await self.reload()

    async def reload(self) -> str | None:
        """Applies the settings file: None when it is, else why it was not (the old server keeps running)."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            self._seen = self._stamp()
            if self._seen is None:
                return None  # no file: nothing to apply
            loop = asyncio.get_running_loop()
            try:
                config = await loop.run_in_executor(None, load, self.path)
            except ConfigError as error:
                self.error = str(error)
                log.warning("the settings file was not applied: %s", error)
                return self.error
            old = self.current
            if config.to_toml() == old.state.runtime.config.to_toml():
                self.error = None
                return None
            try:
                new = await loop.run_in_executor(None, self._build, config, self)
            except Exception as error:
                self.error = f"{type(error).__name__}: {error}"
                log.exception("a server could not be built from the settings file; the old one keeps running")
                return self.error
            await loop.run_in_executor(None, old.state.runtime.halt)
            try:
                if self.run:
                    await loop.run_in_executor(None, new.state.runtime.start)
            except Exception as error:
                # The old runtime is halted and cannot start again: build it afresh from what it ran with, and
                # throw the new one away.
                log.exception("the new settings could not start; the old ones are back")
                self.error = f"{type(error).__name__}: {error}"
                await loop.run_in_executor(None, self._close, new)
                new = await loop.run_in_executor(None, self._build, old.state.runtime.config, self)
                if self.run:
                    await loop.run_in_executor(None, new.state.runtime.start)
            else:
                self.error = None
            self.current = new
            task = asyncio.create_task(self._retire(old))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            log.info("settings applied: the server was swapped without a restart")
            return self.error

    async def _retire(self, old: FastAPI) -> None:
        """Closes a server once it has answered its last request (or after DRAIN seconds)."""
        waited = 0.0
        while self._busy.get(id(old), 0) > 0 and waited < DRAIN:
            await asyncio.sleep(0.1)
            waited += 0.1
        await asyncio.get_running_loop().run_in_executor(None, self._close_stores, old)
        self._busy.pop(id(old), None)

    @staticmethod
    def _close_stores(app: FastAPI) -> None:
        app.state.runtime.close()
        app.state.accounts.close()
