"""The web application: the SDK's LanternApp behind its FastAPI adapter.

The protocol (routes, pairing, tokens, files, events) is the toolkit's; the
bridge brings the library it serves: the hashed files of its sources, and
its accounts, kept in the state directory.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from lantern.server import LanternApp, MemoryLibrary, SqliteAccounts
from lantern.server.fastapi import include

from lantern_bridge.config import Config
from lantern_bridge.runtime import Runtime


def make_app(config: Config, state_dir: Path, *, run: bool = True) -> FastAPI:
    """A Lantern server for `config`; its state (index, accounts) lives in `state_dir`.

    `run=False` leaves the background pass off (tests call `app.state.runtime.cycle()`).
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    library = MemoryLibrary(base="/api/lantern")
    runtime = Runtime(config, state_dir, library)
    accounts = SqliteAccounts(state_dir / "accounts.sqlite3")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if run:
            runtime.start()
        try:
            yield
        finally:
            runtime.stop()
            accounts.close()

    app = FastAPI(title="lantern-bridge", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.runtime = runtime
    include(app, LanternApp(library, uid=config.uid, name=config.name, accounts=accounts))
    return app
