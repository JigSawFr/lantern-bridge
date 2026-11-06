# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The administration pages, under ``/admin/``: server-rendered HTML and a little script.

Nothing is loaded from elsewhere (scripts and styles are served from here, a
policy forbids the rest), no cookie but the session, no referrer sent.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
from collections.abc import AsyncIterator
from importlib import resources
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from jinja2 import Environment, PackageLoader, select_autoescape
from lantern.server import LanternApp
from lantern.server.pairing import Limiter

from lantern_bridge import SPEC_DRAFT, __version__
from lantern_bridge.admin.auth import AdminStore, Session
from lantern_bridge.config import Config
from lantern_bridge.logs import LEVELS, LogRing
from lantern_bridge.runtime import Runtime

log = logging.getLogger("lantern_bridge.admin")

_BODY_LIMIT = 8192
_COOKIE = "lb_session"
_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}
_USER_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
_DEFAULT_SCOPES = ("library", "saves", "cheats", "requests", "sync.read", "sync.write")


def _local(host: str | None) -> bool:
    """Whether an address is the machine itself or on a private network."""
    try:
        address = ipaddress.ip_address((host or "").split("%")[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def _size(count: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if count < 1024 or unit == "TiB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return ""


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    return f"{seconds // 86400} d"


class Admin:
    def __init__(self, config: Config, runtime: Runtime, lantern: LanternApp, ring: LogRing, store: AdminStore) -> None:
        self.config, self.runtime, self.lantern, self.ring, self.store = config, runtime, lantern, ring, store
        self.attempts = Limiter(5, 300.0)
        self.env = Environment(
            loader=PackageLoader("lantern_bridge.admin", "templates"), autoescape=select_autoescape(["html"])
        )
        self.env.filters["size"] = _size
        self.env.filters["when"] = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "-"

    # -- state ---------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        runtime, index = self.runtime, self.runtime.index
        counts, by_source = index.counts(), index.counts_by_source()
        pending_bytes = index.pending_bytes()
        stats = runtime.stats
        rate = stats.hashed_bytes / stats.hashing_seconds if stats.hashing_seconds > 0 else 0.0
        apps, titles = runtime.projection.served()
        sources = []
        for source in self.config.sources:
            seen = stats.sources.get(source.id)
            c = by_source.get(source.id, {"hashed": 0, "pending": 0})
            sources.append(
                {
                    "id": source.id,
                    "path": source.location,
                    "reachable": seen.reachable if seen else None,
                    "scanned": seen.scanned if seen else None,
                    "error": seen.error if seen else None,
                    "hashed": c["hashed"],
                    "pending": c["pending"],
                }
            )
        return {
            "hashed": counts["hashed"],
            "pending": counts["pending"],
            "total": counts["hashed"] + counts["pending"],
            "pending_bytes": pending_bytes,
            "rate": rate,
            "eta": (pending_bytes / rate) if rate > 0 and counts["pending"] else None,
            "apps": apps,
            "titles": titles,
            "sources": sources,
            "waiting": len(self.lantern.desk.waiting()) if self.lantern.desk is not None else 0,
        }

    # -- plumbing ------------------------------------------------------------

    def page(
        self, request: Request, template: str, session: Session | None = None, status: int = 200, **ctx: Any
    ) -> HTMLResponse:
        html = self.env.get_template(template).render(
            session=session, version=__version__, draft=SPEC_DRAFT, name=self.config.name, **ctx
        )
        return HTMLResponse(html, status_code=status, headers=_HEADERS)

    def cookie(self, response: Response, request: Request, token: str | None) -> None:
        if token is None:
            response.delete_cookie(_COOKIE, path="/admin")
            return
        response.set_cookie(
            _COOKIE,
            token,
            max_age=8 * 3600,
            path="/admin",
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )

    def allowed(self, request: Request) -> bool:
        return not self.config.admin_lan_only or _local(request.client.host if request.client else None)

    async def form(self, request: Request) -> dict[str, str]:
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > _BODY_LIMIT:
                return {}
        return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}

    def session(self, request: Request) -> Session | None:
        return self.store.session(request.cookies.get(_COOKIE))


def mount(app: FastAPI, admin: Admin) -> None:
    async def local_only(request: Request) -> None:
        # A dependency of the router, not a middleware: the protocol's own routes (and their streams) are untouched.
        if not admin.allowed(request):
            raise HTTPException(403, "The administration pages answer on the local network only.")

    router = APIRouter(prefix="/admin", dependencies=[Depends(local_only)])
    static = resources.files("lantern_bridge.admin") / "static"

    def redirect(path: str, request: Request, token: str | bool | None = False) -> RedirectResponse:
        response = RedirectResponse("/admin" + path, status_code=303, headers=_HEADERS)
        if token is not False:
            admin.cookie(response, request, token if isinstance(token, str) else None)
        return response

    @router.get("/static/{name}")
    async def asset(name: str) -> Response:
        if name not in ("app.css", "app.js"):
            return Response(status_code=404)
        kind = "text/css" if name.endswith("css") else "text/javascript"
        return Response(
            (static / name).read_bytes(), media_type=kind, headers={**_HEADERS, "Cache-Control": "max-age=3600"}
        )

    @router.get("/setup")
    async def setup_form(request: Request) -> Response:
        if admin.store.setup_code() is None:
            return redirect("/login", request)
        return admin.page(request, "setup.html", error=None)

    @router.post("/setup")
    async def setup_post(request: Request) -> Response:
        fields = await admin.form(request)
        key = request.client.host if request.client else "?"
        if wait := admin.attempts.wait(key):
            return admin.page(request, "setup.html", status=429, error=f"Too many tries: wait {wait:.0f} s.")
        error = admin.store.create(fields.get("code", ""), fields.get("name", ""), fields.get("password", ""))
        if error is not None:
            admin.attempts.record(key)
            return admin.page(request, "setup.html", status=400, error=error)
        log.info("administrator %s created", fields["name"].strip())
        return redirect("/login", request)

    @router.get("/login")
    async def login_form(request: Request) -> Response:
        if not admin.store.configured:
            return redirect("/setup", request)
        return admin.page(request, "login.html", error=None)

    @router.post("/login")
    async def login_post(request: Request) -> Response:
        fields = await admin.form(request)
        key = request.client.host if request.client else "?"
        if wait := admin.attempts.wait(key):
            return admin.page(request, "login.html", status=429, error=f"Too many tries: wait {wait:.0f} s.")
        if not admin.store.check(fields.get("name", ""), fields.get("password", "")):
            admin.attempts.record(key)
            log.warning("a sign-in failed from %s", key)
            return admin.page(request, "login.html", status=401, error="Wrong name or password.")
        token, _ = admin.store.login(fields["name"].strip())
        return redirect("/", request, token)

    async def authed(request: Request) -> tuple[Session | None, dict[str, str], Response | None]:
        """The session, the form of a POST, and the response to give instead when the request is refused."""
        if not admin.store.configured:
            return None, {}, redirect("/setup", request)
        session = admin.session(request)
        if session is None:
            return None, {}, redirect("/login", request)
        fields: dict[str, str] = {}
        if request.method == "POST":
            fields = await admin.form(request)
            if fields.get("csrf") != session.csrf:
                return None, {}, Response("The form is out of date: go back and try again.", 403, headers=_HEADERS)
        return session, fields, None

    @router.post("/logout")
    async def logout(request: Request) -> Response:
        _, _, refused = await authed(request)
        if refused is not None:
            return refused
        admin.store.logout(request.cookies.get(_COOKIE))
        return redirect("/login", request, None)

    @router.get("")
    @router.get("/")
    async def overview(request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        return admin.page(
            request,
            "overview.html",
            session,
            active="overview",
            state=admin.state(),
            uid=admin.config.uid,
            public=admin.config.public,
        )

    @router.get("/api/state")
    async def state(request: Request) -> Response:
        _, _, refused = await authed(request)
        if refused is not None:
            return Response(status_code=401, headers=_HEADERS)
        data = admin.state()
        data["rate_text"] = _size(data["rate"]) + "/s" if data["rate"] else "-"
        data["eta_text"] = _duration(data["eta"]) if data["eta"] else "-"
        data["pending_size_text"] = _size(data["pending_bytes"])
        return Response(json.dumps(data), media_type="application/json", headers=_HEADERS)

    @router.post("/scan")
    async def scan(request: Request) -> Response:
        _, fields, refused = await authed(request)
        if refused is not None:
            return refused
        source = fields.get("source")
        if fields.get("action") == "rehash":
            known = {s.id for s in admin.config.sources}
            if source and source not in known:
                return Response("No such source.", 404, headers=_HEADERS)
            count = admin.runtime.index.rehash(source or None)
            log.info("rehash requested from the pages: %d hashes forgotten (%s)", count, source or "all sources")
        else:
            log.info("a new pass was requested from the pages")
        admin.runtime.wake()
        return redirect("/", request)

    @router.get("/pairings")
    async def pairings(request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        desk = admin.lantern.desk
        waiting = desk.waiting() if desk is not None else []
        accounts = admin.lantern.accounts
        users = [u.name for u in accounts.users()] if accounts is not None else []
        return admin.page(
            request, "pairings.html", session, active="pairings", waiting=waiting, users=users, error=None
        )

    @router.post("/pairings/{pairing_id}")
    async def decide(pairing_id: str, request: Request) -> Response:
        session, fields, refused = await authed(request)
        if refused is not None:
            return refused
        desk, accounts = admin.lantern.desk, admin.lantern.accounts
        entry = desk.by_id(pairing_id) if desk is not None else None
        if desk is None or accounts is None or entry is None:
            return redirect("/pairings", request)
        if fields.get("decision") == "refuse":
            desk.refuse(entry)
            log.info("pairing of %s refused", entry.device)
            return redirect("/pairings", request)
        name = fields.get("user", "").strip()
        if not name or len(name) > 64 or not set(name) <= _USER_CHARS:
            users = [u.name for u in accounts.users()]
            return admin.page(
                request,
                "pairings.html",
                session,
                status=400,
                active="pairings",
                waiting=desk.waiting(),
                users=users,
                error="An account name is letters, digits, - _ and . (64 at most).",
            )
        user = accounts.user(name) or accounts.create_user(name, _DEFAULT_SCOPES)
        granted = desk.approve(entry, user.name)
        log.info("pairing of %s approved for %s: %s", entry.device, user.name, ", ".join(granted) or "no scope")
        return redirect("/pairings", request)

    @router.get("/devices")
    async def devices(request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        accounts = admin.lantern.accounts
        return admin.page(
            request,
            "devices.html",
            session,
            active="devices",
            devices=accounts.devices() if accounts else [],
        )

    @router.post("/devices/{device_id}/revoke")
    async def revoke(device_id: str, request: Request) -> Response:
        _, _, refused = await authed(request)
        if refused is not None:
            return refused
        accounts = admin.lantern.accounts
        if accounts is not None and accounts.revoke(device_id):
            log.info("device %s revoked", device_id)
        return redirect("/devices", request)

    @router.get("/logs")
    async def logs(request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        level = request.query_params.get("level", "INFO").upper()
        level = level if level in LEVELS else "INFO"
        return admin.page(
            request,
            "logs.html",
            session,
            active="logs",
            level=level,
            levels=LEVELS,
            lines=admin.ring.since(0, level, limit=200),
        )

    @router.get("/logs/stream")
    async def stream(request: Request) -> Response:
        _, _, refused = await authed(request)
        if refused is not None:
            return Response(status_code=401, headers=_HEADERS)
        level = request.query_params.get("level", "INFO").upper()
        last = int(request.headers.get("last-event-id") or request.query_params.get("after", "0") or 0)

        async def events() -> AsyncIterator[str]:
            nonlocal last
            beat = 0
            while not await request.is_disconnected():
                for line in admin.ring.since(last, level):
                    last = line.id
                    yield f"id: {line.id}\ndata: {json.dumps(line.to_json())}\n\n"
                    beat = 0
                beat += 1
                if beat >= 15:
                    yield ": keep-alive\n\n"
                    beat = 0
                await asyncio.sleep(1)

        return StreamingResponse(events(), media_type="text/event-stream", headers=_HEADERS)

    app.include_router(router)
