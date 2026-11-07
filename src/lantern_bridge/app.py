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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from lantern.server import LanternApp, SqliteAccounts
from lantern.server.fastapi import include

from lantern_bridge import health
from lantern_bridge.admin import Admin, mount
from lantern_bridge.admin.auth import AdminStore
from lantern_bridge.config import Config
from lantern_bridge.logs import LogRing, install
from lantern_bridge.metrics import render
from lantern_bridge.runtime import Runtime
from lantern_bridge.traffic import TrafficMiddleware
from lantern_bridge.upstream import BridgeLibrary


def make_app(
    config: Config, state_dir: Path, *, run: bool = True, json_logs: bool = False, config_path: Path | None = None
) -> FastAPI:
    """A Lantern server for `config`; its state (index, accounts) lives in `state_dir`.

    `config_path` is the file the settings pages write (by default `config.toml` in `state_dir`).
    `run=False` leaves the background pass off (tests call `app.state.runtime.cycle()`).
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    library = BridgeLibrary(base="/api/lantern")
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
    lantern = LanternApp(
        library,
        uid=config.uid,
        name=config.name,
        accounts=accounts,
        public=config.public,
        saves=runtime.saves,
        encrypted_saves=False,  # the AeroFoil keeps plain zip archives
        requests=runtime.requests,
    )
    runtime.answer_request = lantern.answer_request
    include(app, lantern)

    def identify(token: str) -> tuple[str, str] | None:
        device = accounts.device_of(token)
        return (device.id, f"{device.device} · {device.user}") if device is not None else None

    runtime.traffic.identify = identify
    app.add_middleware(TrafficMiddleware, traffic=runtime.traffic)
    ring = LogRing()
    install(ring, json_lines=json_logs)
    store = AdminStore(state_dir / "admin.json")
    app.state.admin = store
    app.state.logs = ring
    if store.setup_code() is not None:
        logging.getLogger("lantern_bridge.admin").warning(
            "no administrator yet: open /admin/ and enter the setup code %s", store.setup_code()
        )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        """The whole status and nothing else: 200 when the bridge does its job (a warning included), 503 when not."""
        status = health.overall(health.run(runtime))
        return JSONResponse({"status": status}, status_code=503 if status == health.FAIL else 200)

    pages = Admin(config, runtime, lantern, ring, store, config_path=config_path or state_dir / "config.toml")
    app.state.admin_pages = pages
    mount(app, pages)

    @app.get("/metrics", include_in_schema=False)
    async def metrics_page(request: Request) -> Response:
        """The figures for a monitoring tool: off unless the settings say so, and for the local network only."""
        if not config.metrics:
            raise HTTPException(404)
        if not pages.reachable(request):
            raise HTTPException(403)
        return PlainTextResponse(render(runtime), media_type="text/plain; version=0.0.4; charset=utf-8")

    return app
