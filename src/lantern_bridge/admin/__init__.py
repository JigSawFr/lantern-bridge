# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The administration pages, under ``/admin/``: server-rendered HTML and a little script.

Nothing is loaded from elsewhere (scripts and styles are served from here, a
policy forbids the rest), no cookie but the session, no referrer sent.
"""

from __future__ import annotations

import asyncio
import dataclasses
import ipaddress
import json
import logging
import os
import platform
import re
import threading
import time
from collections.abc import AsyncIterator, Iterable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from jinja2 import Environment, PackageLoader, select_autoescape
from lantern.server import LanternApp
from lantern.server.pairing import Limiter

from lantern_bridge import SPEC_DRAFT, __version__, health
from lantern_bridge.admin.auth import AdminStore, Session
from lantern_bridge.admin.i18n import COOKIE as LANG_COOKIE
from lantern_bridge.admin.i18n import LANGUAGES, pick, translator
from lantern_bridge.catalog import search
from lantern_bridge.config import (
    AerofoilSource,
    Config,
    ConfigError,
    FolderSource,
    Login,
    OwnfoilSource,
    ShopSource,
    TinfoilSource,
    check,
    load,
    save,
)
from lantern_bridge.logs import LEVELS, LogRing
from lantern_bridge.runtime import Runtime

log = logging.getLogger("lantern_bridge.admin")

_BODY_LIMIT = 8192
_COOKIE = "lb_session"
_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; font-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}
_USER_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
_DEFAULT_SCOPES = ("library", "saves", "cheats", "requests", "sync.read", "sync.write")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_STATIC = {
    "app.css": "text/css",
    "app.js": "text/javascript",
    "bridge.svg": "image/svg+xml",
    "sora.woff2": "font/woff2",
    "figtree.woff2": "font/woff2",
    "jetbrains-mono.woff2": "font/woff2",
}
#: What a settings page says after it saved (a fixed word in the address, never a text).
_DONE = {
    "saved": "Saved.",
    "added": "Source added.",
    "removed": "Source removed.",
    "moved": "Source moved.",
    "tied": "Login tied.",
    "untied": "Login removed.",
    "queued": "Hashing again: these files go first.",
    "done": "Done.",
}
_KINDS = {"folder": FolderSource, "tinfoil": TinfoilSource, "ownfoil": OwnfoilSource, "aerofoil": AerofoilSource}


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
    def __init__(
        self,
        config: Config,
        runtime: Runtime,
        lantern: LanternApp,
        ring: LogRing,
        store: AdminStore,
        *,
        config_path: Path,
    ) -> None:
        self.config, self.runtime, self.lantern, self.ring, self.store = config, runtime, lantern, ring, store
        self.config_path = config_path
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

    # -- settings file -------------------------------------------------------

    def file(self) -> Config:
        """The settings as the file has them (the running ones when there is no file yet)."""
        return load(self.config_path) if self.config_path.exists() else deepcopy(self.config)

    def write(self, config: Config) -> None:
        """Checks, then writes; ConfigError for what the reader would refuse."""
        save(self.config_path, check(config))

    def restart_needed(self) -> bool:
        try:
            return self.file().to_toml() != self.config.to_toml()
        except ConfigError:
            return True

    def aerofoil(self, config: Config) -> AerofoilSource | None:
        return next((s for s in config.sources if isinstance(s, AerofoilSource)), None)

    # -- plumbing ------------------------------------------------------------

    def lang(self, request: Request) -> str:
        return pick(request.cookies.get(LANG_COOKIE), request.headers.get("accept-language"))

    def tr(self, request: Request) -> Any:
        return translator(self.lang(request))

    def page(
        self, request: Request, template: str, session: Session | None = None, status: int = 200, **ctx: Any
    ) -> HTMLResponse:
        lang = self.lang(request)
        html = self.env.get_template(template).render(
            session=session,
            version=__version__,
            draft=SPEC_DRAFT,
            name=self.config.name,
            developer=self.config.developer,
            lang=lang,
            languages=LANGUAGES,
            here=request.url.path if request.url.path.startswith("/admin") else "/admin/",
            _=translator(lang),
            **ctx,
        )
        return HTMLResponse(html, status_code=status, headers=_HEADERS)

    def diagnostic(self) -> dict[str, Any]:
        """What helps to understand a bridge from far away, with no secret: no password, no address's login,
        no token; a variable's name and whether it is set, never its value."""
        import lantern

        state = self.state()
        sources = []
        for source in self.config.sources:
            row: dict[str, Any] = {"id": source.id, "kind": type(source).__name__.removesuffix("Source").lower()}
            if isinstance(source, ShopSource):
                row["address"] = re.sub(r"//[^/@]*@", "//", source.url)
                row["user_set"] = source.user is not None
                row["password_env"] = source.password_env
                row["password_set"] = source.password_env in os.environ if source.password_env else None
            else:
                row["path"] = str(source.path)
            if isinstance(source, AerofoilSource):
                row["logins"] = len(source.logins)
            sources.append(row)
        return {
            "schema": 1,
            "generated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bridge": __version__,
            "lantern_sdk": getattr(lantern, "__version__", None),
            "draft": SPEC_DRAFT,
            "python": platform.python_version(),
            "system": f"{platform.system()} {platform.machine()}",
            "uptime_seconds": int(time.time() - self.runtime.stats.started),
            "public": self.config.public,
            "admin_lan_only": self.config.admin_lan_only,
            "remote_rate": self.config.remote_rate,
            "sources": sources,
            "state": {k: v for k, v in state.items() if k != "sources"},
            "source_states": state["sources"],
            "restart_needed": self.restart_needed(),
            "recent_problems": [
                {"time": line.time, "level": line.level, "message": line.message}
                for line in self.ring.since(0, "WARNING", limit=200)
            ],
        }

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
        kind = _STATIC.get(name)
        if kind is None:
            return Response(status_code=404)
        return Response(
            (static / name).read_bytes(), media_type=kind, headers={**_HEADERS, "Cache-Control": "max-age=3600"}
        )

    @router.post("/lang")
    async def language(request: Request) -> Response:
        fields = await admin.form(request)
        target = fields.get("next", "")
        back = target if target.startswith("/admin") and "//" not in target and "\\" not in target else "/admin/"
        response = RedirectResponse(back, status_code=303, headers=_HEADERS)
        if fields.get("lang") in LANGUAGES:
            response.set_cookie(LANG_COOKIE, fields["lang"], max_age=365 * 86400, path="/admin", samesite="strict")
        return response

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
            return admin.page(
                request,
                "setup.html",
                status=429,
                error=admin.tr(request)("Too many tries: wait {wait} s.", wait=f"{wait:.0f}"),
            )
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
            return admin.page(
                request,
                "login.html",
                status=429,
                error=admin.tr(request)("Too many tries: wait {wait} s.", wait=f"{wait:.0f}"),
            )
        if not admin.store.check(fields.get("name", ""), fields.get("password", "")):
            admin.attempts.record(key)
            log.warning("a sign-in failed from %s", key)
            return admin.page(request, "login.html", status=401, error=admin.tr(request)("Wrong name or password."))
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
            checks=health.run(admin.runtime),
            state=admin.state(),
            uid=admin.config.uid,
            public=admin.config.public,
            restart=admin.restart_needed(),
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

    def icons_of(title_ids: Iterable[str]) -> set[str]:
        """The titles, among these, whose sheet has an icon the bridge holds."""
        projection = admin.runtime.projection
        return {i for i in title_ids if (sheet := projection.title(i)) is not None and sheet.icon is not None}

    @router.get("/library/export")
    async def library_export(request: Request) -> Response:
        """The library as a file to keep: ?format=json (default, with a schema number) or csv."""
        _, _, refused = await authed(request)
        if refused is not None:
            return refused
        catalog = admin.runtime.browser.catalog()
        stamp = time.strftime("%Y-%m-%d", time.gmtime())
        if request.query_params.get("format") == "csv":
            body, kind, extension = catalog.export_csv(), "text/csv; charset=utf-8", "csv"
        else:
            document = catalog.export(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            body, kind, extension = json.dumps(document, indent=2) + "\n", "application/json", "json"
        return Response(
            body,
            media_type=kind,
            headers={**_HEADERS, "Content-Disposition": f'attachment; filename="library-{stamp}.{extension}"'},
        )

    @router.get("/library/checks")
    async def library_checks(request: Request) -> Response:
        """Duplicates and updates without their game."""
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        return admin.page(
            request, "checks.html", session, active="library", issues=admin.runtime.browser.catalog().issues()
        )

    @router.get("/library/ignored")
    async def ignored_files(request: Request) -> Response:
        """The packages of the folders that the library leaves out, and why."""
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        files = [(source, path, size) for source, found in admin.runtime.stats.ignored.items() for path, size in found]
        return admin.page(request, "ignored.html", session, active="library", files=sorted(files))

    @router.get("/library/{title_id}/image/{kind}")
    async def title_image(title_id: str, kind: str, request: Request) -> Response:
        """An image of a title's sheet (icon, banner or shot-N), read from what the library serves."""
        _, _, refused = await authed(request)
        if refused is not None:
            return refused
        sheet = admin.runtime.projection.title(title_id.upper())
        shots = sheet.screenshots or [] if sheet is not None else []
        picked = None
        if sheet is not None and kind in ("icon", "banner"):
            picked = sheet.icon if kind == "icon" else sheet.banner
        elif sheet is not None and kind.startswith("shot-") and kind[5:].isdigit() and int(kind[5:]) < len(shots):
            picked = shots[int(kind[5:])]
        ref = admin.runtime.projection.library.media(picked.url.rsplit("/", 1)[-1]) if picked is not None else None
        if ref is None or ref.data is None:
            return Response("No such image.", 404, headers=_HEADERS)
        return Response(
            ref.data,
            media_type=ref.content_type,
            headers={**_HEADERS, "Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
        )

    @router.get("/library")
    async def library_page(request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        q = request.query_params
        catalog = admin.runtime.browser.catalog()
        number = q.get("page", "1")
        found = search(
            catalog,
            q=q.get("q", "")[:100],
            kind=q.get("type", ""),
            source=q.get("source", ""),
            state=q.get("state", ""),
            genre=q.get("genre", ""),
            sort=q.get("sort", "name"),
            page=int(number) if number.isdigit() else 1,
        )
        return admin.page(
            request,
            "library.html",
            session,
            active="library",
            found=found,
            counts=catalog.counts(),
            genres=catalog.genres(),
            icons=icons_of(t.title_id for t in found.titles),
            ignored=sum(len(v) for v in admin.runtime.stats.ignored.values()),
            issues=len(catalog.issues()),
            sources=[s.id for s in admin.config.sources],
            f={k: q.get(k, "") for k in ("q", "type", "source", "state", "genre", "sort")},
        )

    def title_view(request: Request, session: Session | None, title_id: str, done: str | None = None) -> Response:
        title = admin.runtime.browser.catalog().titles.get(title_id.upper())
        if title is None:
            return admin.page(request, "missing.html", session, status=404, active="library")
        cheats = [c for c in admin.runtime.projection.library.cheats() if c.title_id.upper() == title.title_id]
        sheet = admin.runtime.projection.title(title.title_id)
        return admin.page(
            request,
            "title.html",
            session,
            active="library",
            title=title,
            sheet=sheet,
            has_image=bool(sheet is not None and sheet.icon is not None),
            shots=len(sheet.screenshots or []) if sheet is not None else 0,
            cheats=cheats,
            timings=admin.runtime.stats.timings,
            atlas=admin.runtime.atlas.get(title.title_id) if admin.runtime.atlas is not None else None,
            message=admin.tr(request)(_DONE[done]) if done else None,
        )

    @router.get("/library/{title_id}")
    async def title_page(title_id: str, request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        return title_view(request, session, title_id)

    @router.post("/library/{title_id}/rehash")
    async def title_rehash(title_id: str, request: Request) -> Response:
        session, fields, refused = await authed(request)
        if refused is not None:
            return refused
        title = admin.runtime.browser.catalog().titles.get(title_id.upper())
        if title is None:
            return admin.page(request, "missing.html", session, status=404, active="library")
        mine = {(f.source, f.path) for f in title.files}
        if fields.get("path"):  # one file; it has to be one of this title's
            wanted = [(fields.get("source", ""), fields["path"])]
            if wanted[0] not in mine:
                return Response("No such file in this title.", 404, headers=_HEADERS)
        else:
            wanted = sorted(mine)
        count = admin.runtime.rehash(wanted)
        log.info("rehash of %s requested from the pages: %d file(s)", title.title_id, count)
        return title_view(request, session, title.title_id, "queued")

    def activity_data() -> dict[str, Any]:
        traffic = admin.runtime.traffic
        transfers, clients, _ = traffic.snapshot()
        now = time.monotonic()
        days = admin.runtime.history.days(14)
        return {
            "days": days,
            "days_max": max((d.sent for d in days), default=0),
            "transfers": transfers,
            "clients": clients,
            "rates": traffic.rates(),
            "totals": traffic.totals,
            "uptime": time.time() - admin.runtime.stats.started,
            "state": admin.state(),
            "now": now,
        }

    @router.get("/activity")
    async def activity(request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        template = "_activity.html" if request.query_params.get("fragment") else "activity.html"
        return admin.page(request, template, session, active="activity", **activity_data())

    def developer_only() -> None:
        if not admin.config.developer:
            raise HTTPException(404)

    @router.get("/developer")
    async def developer_page(request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        developer_only()
        runtime = admin.runtime
        _, _, recent = runtime.traffic.snapshot()
        slow = sorted(runtime.stats.timings.items(), key=lambda kv: -kv[1][0])[:15]
        library = runtime.projection.library
        journal = library.journal()
        return admin.page(
            request,
            "developer.html",
            session,
            active="developer",
            recent=recent[:200],
            slow=slow,
            process={
                "pid": os.getpid(),
                "python": platform.python_version(),
                "platform": platform.platform(),
                "threads": threading.active_count(),
                "uptime": time.time() - runtime.stats.started,
                "apps": len(library.apps()),
                "titles": len(runtime.browser.catalog().titles),
                "journal": journal.position() if journal is not None else None,
                "cycle": admin.runtime.stats.hashed_files,
            },
            state=admin.state(),
            message=admin.tr(request)(_DONE[request.query_params["m"]])
            if request.query_params.get("m") in _DONE
            else None,
        )

    @router.get("/developer/title.json")
    async def developer_title(request: Request) -> Response:
        _, _, refused = await authed(request)
        if refused is not None:
            return Response(status_code=401, headers=_HEADERS)
        developer_only()
        title_id = request.query_params.get("id", "").strip().upper()
        runtime = admin.runtime
        title = runtime.browser.catalog().titles.get(title_id)
        sheet = runtime.projection.title(title_id)
        apps = [a for a in runtime.projection.library.apps() if a.title_id.upper() == title_id]
        body = {
            "title_id": title_id,
            "known": title is not None,
            "files": [dataclasses.asdict(f) for f in title.files] if title is not None else [],
            "sheet": sheet.to_json() if sheet is not None else None,
            "apps": [a.to_json() for a in apps],
        }
        return Response(json.dumps(body, indent=2), media_type="application/json", headers=_HEADERS)

    @router.post("/developer/{action}")
    async def developer_action(action: str, request: Request) -> Response:
        _, _, refused = await authed(request)
        if refused is not None:
            return refused
        developer_only()
        if action == "forget":
            admin.runtime.browser.forget()
            log.info("developer: the catalogue cache was cleared")
        elif action == "scan":
            admin.runtime.wake()
            log.info("developer: a new pass was requested")
        else:
            raise HTTPException(404)
        return redirect("/developer?m=done", request)

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
                error=admin.tr(request)("An account name is letters, digits, - _ and . (64 at most)."),
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

    # -- settings --------------------------------------------------------------

    def settings_page(
        request: Request,
        session: Session | None,
        status: int = 200,
        tested: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> Response:
        t = admin.tr(request)
        try:
            file = admin.file()
        except ConfigError as problem:
            return admin.page(
                request,
                "config.html",
                session,
                status=500,
                active="config",
                file=admin.config,
                sources=[],
                restart=True,
                message=None,
                error=str(problem),
                tested=None,
            )
        sources = [
            {
                "id": x.id,
                "kind": type(x).__name__.removesuffix("Source").lower(),
                "location": x.location,
                "user": getattr(x, "user", None),
                "password_env": getattr(x, "password_env", None),
                "password_set": bool(getattr(x, "password_env", None)) and getattr(x, "password_env", "") in os.environ,
            }
            for x in file.sources
        ]
        done = _DONE.get(request.query_params.get("m", ""))
        return admin.page(
            request,
            "config.html",
            session,
            status=status,
            active="config",
            file=file,
            sources=sources,
            restart=admin.restart_needed(),
            message=t(done) if done else None,
            error=error,
            tested=tested,
            atlas_info=admin.runtime.atlas,
        )

    async def change(request: Request, edit: Any, done: str) -> Response:
        """Applies `edit(config)` to the settings file and answers with the page (or the problem)."""
        session, fields, refused = await authed(request)
        if refused is not None:
            return refused
        t = admin.tr(request)
        try:
            config = admin.file()
            problem = edit(config, fields)
            if problem is None:
                admin.write(config)
        except ConfigError as error:
            problem = str(error)
        except OSError as error:
            problem = f"{error.strerror or error}"
        if problem is not None:
            return settings_page(request, session, 400, error=t(problem) if problem in _FIXED else problem)
        log.info("settings changed from the pages: %s", done)
        return redirect(f"/config?m={done}", request)

    _FIXED = {"No such source.", "Only an AeroFoil source has logins.", "Choose a number of devices from 1 to 20."}

    @router.get("/config")
    async def config_page(request: Request) -> Response:
        session, _, refused = await authed(request)
        return refused if refused is not None else settings_page(request, session)

    @router.post("/config/settings")
    async def config_settings(request: Request) -> Response:
        def edit(config: Config, f: dict[str, str]) -> str | None:
            try:
                rate = float(f.get("remote_rate", "10") or 0)
            except ValueError as error:
                raise ConfigError("the rate is a number") from error
            if not 0 <= rate <= 1000:
                raise ConfigError("the rate is between 0 and 1000")
            config.name = f.get("name", "").strip()
            config.remote_rate = rate
            config.public = "public" in f
            config.admin_lan_only = "admin_lan_only" in f
            config.developer = "developer" in f
            config.atlas = "atlas" in f
            return None

        return await change(request, edit, "saved")

    @router.post("/config/sources")
    async def config_add(request: Request) -> Response:
        def edit(config: Config, f: dict[str, str]) -> str | None:
            kind, sid, where = f.get("kind", ""), f.get("id", "").strip(), f.get("where", "").strip()
            cls = _KINDS.get(kind)
            if cls is None:
                raise ConfigError("this kind of source is not supported")
            if kind == "folder":
                config.sources.append(FolderSource(id=sid, path=Path(where)))
                return None
            user, env = f.get("user", "").strip() or None, f.get("password_env", "").strip() or None
            if env is not None and not _ENV_NAME.match(env):
                raise ConfigError("a variable's name is letters, digits and _")
            config.sources.append(cls(id=sid, url=where, user=user, password_env=env))
            return None

        return await change(request, edit, "added")

    def find(config: Config, sid: str) -> int | None:
        return next((n for n, x in enumerate(config.sources) if x.id == sid), None)

    @router.post("/config/sources/{sid}/remove")
    async def config_remove(sid: str, request: Request) -> Response:
        def edit(config: Config, f: dict[str, str]) -> str | None:
            at = find(config, sid)
            if at is None:
                return "No such source."
            del config.sources[at]
            return None

        return await change(request, edit, "removed")

    async def move(sid: str, request: Request, step: int) -> Response:
        def edit(config: Config, f: dict[str, str]) -> str | None:
            at = find(config, sid)
            if at is None:
                return "No such source."
            to = at + step
            if 0 <= to < len(config.sources):
                config.sources[at], config.sources[to] = config.sources[to], config.sources[at]
            return None

        return await change(request, edit, "moved")

    @router.post("/config/sources/{sid}/up")
    async def config_up(sid: str, request: Request) -> Response:
        return await move(sid, request, -1)

    @router.post("/config/sources/{sid}/down")
    async def config_down(sid: str, request: Request) -> Response:
        return await move(sid, request, 1)

    @router.post("/config/sources/{sid}/test")
    async def config_test(sid: str, request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        t = admin.tr(request)
        try:
            at = find(admin.file(), sid)
            source = admin.file().sources[at] if at is not None else None
        except ConfigError as error:
            return settings_page(request, session, 500, error=str(error))
        if source is None:
            return settings_page(request, session, 404, error=t("No such source."))
        if isinstance(source, FolderSource):
            ok = source.path.is_dir() and os.access(source.path, os.R_OK | os.X_OK)
            text = t("The folder is there and can be read.") if ok else t("The folder cannot be read.")
            return settings_page(request, session, tested={"id": sid, "ok": ok, "text": text})
        shop = admin.runtime.open_shop(source)

        def probe() -> tuple[bool, str]:
            from lantern_bridge.sources.tinfoil import UpstreamError

            try:
                first = next(iter(shop.listing()), None)
            except UpstreamError as error:
                return False, f"{t('The shop did not answer')}: {error}"
            finally:
                shop.close()
            return True, t("The shop answers; it lists files.") if first else t("The shop answers; it lists no file.")

        ok, text = await asyncio.to_thread(probe)
        return settings_page(request, session, tested={"id": sid, "ok": ok, "text": text})

    # -- accounts --------------------------------------------------------------

    def accounts_page(
        request: Request, session: Session | None, status: int = 200, error: str | None = None
    ) -> Response:
        t = admin.tr(request)
        accounts = admin.lantern.accounts
        try:
            file = admin.file()
        except ConfigError:
            file = admin.config
        aero = admin.aerofoil(file)
        logins = {x.account: x for x in aero.logins} if aero else {}
        rows = []
        for user in accounts.users() if accounts is not None else []:
            login = logins.get(user.name)
            rows.append(
                {
                    "name": user.name,
                    "state": user.state,
                    "scopes": list(user.scopes),
                    "devices": len(accounts.devices(user.name)) if accounts is not None else 0,
                    "login": {
                        "user": login.user,
                        "password_env": login.password_env,
                        "set": login.password_env in os.environ,
                    }
                    if login
                    else None,
                }
            )
        done = _DONE.get(request.query_params.get("m", ""))
        return admin.page(
            request,
            "accounts.html",
            session,
            status=status,
            active="accounts",
            rows=rows,
            aero={"id": aero.id, "user": aero.user} if aero else None,
            restart=admin.restart_needed(),
            message=t(done) if done else None,
            error=error,
        )

    @router.get("/accounts")
    async def accounts_get(request: Request) -> Response:
        session, _, refused = await authed(request)
        return refused if refused is not None else accounts_page(request, session)

    @router.post("/accounts/{account}/login")
    async def account_login(account: str, request: Request) -> Response:
        def edit(config: Config, f: dict[str, str]) -> str | None:
            aero = admin.aerofoil(config)
            known = admin.lantern.accounts is not None and admin.lantern.accounts.user(account) is not None
            user, env = f.get("user", "").strip(), f.get("password_env", "").strip()
            if aero is None:
                return "Only an AeroFoil source has logins."
            if not known or not user or len(user) > 128 or not _ENV_NAME.match(env):
                raise ConfigError("an account that exists, an AeroFoil user and the name of a variable are needed")
            kept = (*(x for x in aero.logins if x.account != account), Login(account, user, env))
            config.sources[config.sources.index(aero)] = dataclasses.replace(aero, logins=kept)
            return None

        response = await change(request, edit, "tied")
        return _to_accounts(response)

    @router.post("/accounts/{account}/login/remove")
    async def account_unlogin(account: str, request: Request) -> Response:
        def edit(config: Config, f: dict[str, str]) -> str | None:
            aero = admin.aerofoil(config)
            if aero is None:
                return "Only an AeroFoil source has logins."
            kept = tuple(x for x in aero.logins if x.account != account)
            config.sources[config.sources.index(aero)] = dataclasses.replace(aero, logins=kept)
            return None

        return _to_accounts(await change(request, edit, "untied"))

    def _to_accounts(response: Response) -> Response:
        # `change` answers a redirect to the settings; the account pages are where these forms live.
        if response.status_code == 303:
            return RedirectResponse(response.headers["location"].replace("/config", "/accounts"), 303, headers=_HEADERS)
        return response

    # -- invitations -----------------------------------------------------------

    def invitations_page(
        request: Request, session: Session | None, status: int = 200, error: str | None = None, created: Any = None
    ) -> Response:
        accounts = admin.lantern.accounts
        return admin.page(
            request,
            "invitations.html",
            session,
            status=status,
            active="invitations",
            error=error,
            created=created,
            invitations=accounts.invitations() if accounts is not None else [],
            users=[u.name for u in accounts.users()] if accounts is not None else [],
        )

    @router.get("/invitations")
    async def invitations_get(request: Request) -> Response:
        session, _, refused = await authed(request)
        return refused if refused is not None else invitations_page(request, session)

    @router.post("/invitations")
    async def invitation_create(request: Request) -> Response:
        session, fields, refused = await authed(request)
        if refused is not None:
            return refused
        t = admin.tr(request)
        accounts = admin.lantern.accounts
        if accounts is None:
            return redirect("/", request)
        name = fields.get("user", "").strip()
        if not name or len(name) > 64 or not set(name) <= _USER_CHARS:
            return invitations_page(
                request, session, 400, t("An account name is letters, digits, - _ and . (64 at most).")
            )
        try:
            devices, days = int(fields.get("devices", "1")), int(fields.get("days", "7"))
        except ValueError:
            devices, days = 0, 0
        if not 1 <= devices <= 20 or days not in (1, 7, 30):
            return invitations_page(request, session, 400, t("Choose a number of devices from 1 to 20."))
        user = accounts.user(name) or accounts.create_user(name, _DEFAULT_SCOPES)
        until = (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        note = fields.get("note", "").strip()[:200] or None
        invitation = accounts.create_invitation(user.name, user.scopes, until, devices, note=note)
        log.info("an invitation for %s was created from the pages", user.name)
        return invitations_page(request, session, created=invitation)

    @router.post("/invitations/{invitation_id}/revoke")
    async def invitation_revoke(invitation_id: str, request: Request) -> Response:
        _, _, refused = await authed(request)
        if refused is not None:
            return refused
        accounts = admin.lantern.accounts
        if accounts is not None and accounts.revoke_invitation(invitation_id):
            log.info("invitation %s revoked", invitation_id)
        return redirect("/invitations", request)

    # -- diagnostic ------------------------------------------------------------

    @router.get("/diagnostic.json")
    async def diagnostic(request: Request) -> Response:
        _, _, refused = await authed(request)
        if refused is not None:
            return refused
        body = json.dumps(admin.diagnostic(), indent=2, ensure_ascii=False)
        return Response(
            body,
            media_type="application/json",
            headers={**_HEADERS, "Content-Disposition": 'attachment; filename="lantern-bridge-diagnostic.json"'},
        )

    app.include_router(router)
