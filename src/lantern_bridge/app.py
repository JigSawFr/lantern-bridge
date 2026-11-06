"""The web application: the SDK's LanternApp behind its FastAPI adapter.

The protocol (routes, pairing, tokens, files, events) is the toolkit's; the
bridge brings the library it serves: the hashed files of its sources, and
its accounts, kept in the state directory.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from lantern.server import LanternApp, MemoryLibrary, SqliteAccounts
from lantern.server.fastapi import include

from lantern_bridge.admin import Admin, mount
from lantern_bridge.admin.auth import AdminStore
from lantern_bridge.config import Config
from lantern_bridge.logs import LogRing, install
from lantern_bridge.runtime import Runtime


def make_app(config: Config, state_dir: Path, *, run: bool = True, json_logs: bool = False) -> FastAPI:
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
    lantern = LanternApp(library, uid=config.uid, name=config.name, accounts=accounts, public=config.public)
    include(app, lantern)
    ring = LogRing()
    install(ring, json_lines=json_logs)
    store = AdminStore(state_dir / "admin.json")
    app.state.admin = store
    app.state.logs = ring
    if store.setup_code() is not None:
        logging.getLogger("lantern_bridge.admin").warning(
            "no administrator yet: open /admin/ and enter the setup code %s", store.setup_code()
        )
    mount(app, Admin(config, runtime, lantern, ring, store))
    return app
