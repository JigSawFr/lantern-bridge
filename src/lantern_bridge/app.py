"""The web application: the SDK's LanternApp behind its FastAPI adapter.

The protocol (routes, pairing, tokens, files, events) is the toolkit's; the
bridge brings the library it serves. For now that library is an empty one:
the sources come in the following pull requests."""

from __future__ import annotations

from fastapi import FastAPI
from lantern.server import LanternApp, MemoryLibrary
from lantern.server.fastapi import include


def make_app(*, name: str, uid: str) -> FastAPI:
    """A Lantern server called `name`, whose `uid` is a random UUID (version 4 or 7) that never changes."""
    app = FastAPI(title="lantern-bridge", docs_url=None, redoc_url=None, openapi_url=None)
    include(app, LanternApp(MemoryLibrary(base="/api/lantern"), uid=uid, name=name))
    return app
