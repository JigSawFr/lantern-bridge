# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The administration pages, under ``/admin/``: server-rendered HTML and a little script.

Nothing is loaded from elsewhere (scripts and styles are served from here, a
policy forbids the rest), no cookie but the session, no referrer sent.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import csv
import dataclasses
import hashlib
import io
import ipaddress
import json
import logging
import os
import platform
import re
import secrets
import sqlite3
import tempfile
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
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from jinja2 import Environment, PackageLoader, select_autoescape
from lantern.server import AccountError, LanternApp
from lantern.server.pairing import Limiter
from markupsafe import Markup
from starlette.background import BackgroundTask

from lantern_bridge import SPEC_DRAFT, __version__, backup, health, releases, selfcheck
from lantern_bridge.admin import attention, nav, netguard, presets
from lantern_bridge.admin.auth import ROLES, AdminStore, Session
from lantern_bridge.admin.i18n import COOKIE as LANG_COOKIE
from lantern_bridge.admin.i18n import HTML_LANG, LANGUAGES, NAMES, flag, pick, translator
from lantern_bridge.admin.permissions import may
from lantern_bridge.catalog import FIRST_DOWN, KINDS, SORTS, Catalog, TitleInfo, search
from lantern_bridge.compress import CONVERSIONS
from lantern_bridge.config import (
    AerofoilSource,
    Config,
    ConfigError,
    FolderSource,
    HomebrewRepo,
    Login,
    OwnfoilSource,
    ShopSource,
    Source,
    TinfoilSource,
    changes,
    check,
    load,
    parse_mqtt,
    parse_text,
    parse_webhook,
    save,
)
from lantern_bridge.hashing import sha_instructions, workers
from lantern_bridge.history import MAX_EVENTS
from lantern_bridge.logs import LEVELS, LogRing
from lantern_bridge.runtime import Runtime

log = logging.getLogger("lantern_bridge.admin")

_BODY_LIMIT = 8192
_COOKIE = "lb_session"
_THEME_COOKIE = "lb_theme"
_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; font-src 'self'; manifest-src 'self'; worker-src 'self'; form-action 'self'; base-uri 'none'; "
    "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}
_USER_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_STATIC = {
    "app.css": "text/css",
    "app.js": "text/javascript",
    "theme.js": "text/javascript",
    "bridge.svg": "image/svg+xml",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "manifest.webmanifest": "application/manifest+json",
    "sora.woff2": "font/woff2",
    "figtree.woff2": "font/woff2",
    "jetbrains-mono.woff2": "font/woff2",
    "twemoji-flags.woff2": "font/woff2",
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
    "deleted": "The file was deleted from the disk.",
    "converting": "Queued: the conversion starts when the ones before it are done.",
    "ended": "The session was ended.",
    "operator_added": "Operator added.",
    "operator_changed": "Operator changed.",
    "operator_removed": "Operator removed.",
    "password_changed": "Password changed.",
    "account_added": "Account added.",
    "account_changed": "Account changed.",
    "account_deleted": "Account deleted.",
    "done": "Done.",
    "scan": "Reading the sources again.",
    "undone": "The previous settings are back. Restart the bridge to apply them.",
    "imported": "The settings were imported.",
    "homebrew_added": "The repository is added: its latest release is read in a moment.",
    "homebrew_removed": "The repository is removed.",
    "homebrew_checking": "Reading GitHub again.",
}
#: The kinds of the journal, in the order of its filter.
_JOURNAL = (
    "started",
    "scan",
    "source_down",
    "source_up",
    "update",
    "settings",
    "paired",
    "pairing_refused",
    "revoked",
    "restored",
    "deleted",
    "converted",
    "signin_blocked",
    "session_ended",
    "operator_added",
    "operator_changed",
    "operator_removed",
    "password_changed",
    "account_added",
    "account_changed",
    "account_deleted",
)


def _attachment(extension: str) -> dict[str, str]:
    return {"Content-Disposition": f'attachment; filename="lantern-bridge-journal.{extension}"'}


def iso_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def _csv_safe(value: str) -> str:
    """A spreadsheet runs a cell that starts with = + - @ as a formula: a quote in front keeps it text."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


#: What the live stream may follow: the pages' fragments and the state. Each is asked again as the same session.
#: Live streams one session may keep open at once (a page opens one per block it follows).
_STREAMS_PER_SESSION = 8
_BULK = 200  # titles one bulk action or one selection export takes
_NOT_READY = {
    "no_tool": "The nsz tool is not installed.",
    "no_keys": "No prod.keys in the configuration folder.",
}
_BUSY, _IDLE, _SETTLE, _BEAT = (
    1.0,
    5.0,
    0.25,
    15.0,
)  # seconds: look again while busy, while idle, after a change; keep-alive
_FOLLOWED = (
    "/admin",
    "/admin/",
    "/admin/activity",
    "/admin/events",
    "/admin/start",
    "/admin/api/state",
    "/admin/api/badges",
    "/admin/tasks",
)


async def _ask(request: Request, target: str) -> str | None:
    """The body that `target` would answer to this very request (same cookies, same checks), or None if it fails."""
    path, _, query = target.partition("?")
    scope = {
        key: request.scope[key]
        for key in ("type", "asgi", "http_version", "scheme", "server", "client", "root_path", "headers")
        if key in request.scope
    }
    scope.update(method="GET", path=path, raw_path=path.encode(), query_string=query.encode(), replay=True)
    answer: dict[str, Any] = {"body": b"", "status": 0}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            answer["status"] = message["status"]
        elif message["type"] == "http.response.body":
            answer["body"] += message.get("body", b"")

    await request.app(scope, receive, send)
    return answer["body"].decode("utf-8", "replace") if answer["status"] == 200 else None


_KINDS = {"folder": FolderSource, "tinfoil": TinfoilSource, "ownfoil": OwnfoilSource, "aerofoil": AerofoilSource}


def is_local(host: str | None) -> bool:
    """Whether an address is the machine itself or on a private network."""
    try:
        address = ipaddress.ip_address((host or "").split("%")[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def is_loopback(host: str | None) -> bool:
    try:
        return ipaddress.ip_address((host or "").split("%")[0]).is_loopback
    except ValueError:
        return False


def _when(stamp: float | None) -> Markup | str:
    """A moment as its exact local time; the page script turns a recent one into "3 min ago" (the exact time stays
    in the tooltip). Without the script the exact time is what is read."""
    if not stamp:
        return "-"
    exact = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
    return Markup('<time class="ago" datetime="{}" title="{}">{}</time>').format(
        datetime.fromtimestamp(stamp, UTC).isoformat(timespec="seconds"), exact, exact
    )


def _size(count: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if count < 1024 or unit == "TiB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return ""


def _estimate_text(size: int, expected: int) -> str:
    """A result's expected size and how it compares with the source ("170.2 MiB, -30 %")."""
    change = round((expected / size - 1) * 100) if size else 0
    return f"{_size(expected)}, {'-' if change < 0 else '+'}{abs(change)} %"


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
        self.reloader: Any = None  # the server that swaps itself for a new one when the settings change
        self.networks = netguard.parse_networks(config.admin_allow)
        self.proxies = netguard.parse_networks(config.trusted_proxies)
        self.attempts = Limiter(5, 300.0)  # wrong passwords, per address
        self.accounts = Limiter(30, 900.0)  # and per name, wide enough that a stranger cannot lock the real one out
        self.streams: dict[str, int] = {}  # open /admin/live streams, per session
        self.env = Environment(
            loader=PackageLoader("lantern_bridge.admin", "templates"), autoescape=select_autoescape(["html"])
        )
        self.env.filters["size"] = _size
        self.env.filters["day"] = lambda t: time.strftime("%Y-%m-%d", time.localtime(t))
        self.env.filters["clock"] = lambda t: time.strftime("%H:%M:%S", time.localtime(t))
        self.env.filters["iso"] = lambda t: datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")
        self.env.filters["when"] = _when
        self.env.filters["duration"] = _duration

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
                    "free": seen.free if seen else None,
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

    @property
    def previous_path(self) -> Path:
        return self.config_path.with_name(self.config_path.name + ".previous")

    def write(self, config: Config) -> list[str]:
        """Checks, then writes and answers what changed (see `config.changes`); ConfigError for what the reader
        would refuse.

        The file it replaces is kept beside it (`.previous`, one level) so that the pages can take the last change
        back. Writing the same settings again keeps nothing new.
        """
        checked = check(config)
        done: list[str] = changes(self.config, checked)  # no file yet: the running settings are what changes
        if self.config_path.exists():
            try:
                before = load(self.config_path)
            except ConfigError:
                before = None  # a file the reader refuses is not worth keeping: save() says why
            if before is not None:
                done = changes(before, checked)
            if before is not None and before.to_toml() != checked.to_toml():
                keep = self.previous_path
                fd = os.open(keep, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(before.to_toml())
        save(self.config_path, checked)
        return done

    def audit(self, session: Any, done: list[str], how: str) -> None:
        """Puts in the journal who changed the settings, from where, and what (never a value that may be private)."""
        who = f"{session.name} ({session.role})"
        where = f" from {session.address}" if session.address else ""
        what = ", ".join(done) if done else how
        self.runtime.history.event("settings", subject=who[:64], detail=f"{how}{where}: {what}")

    def undo(self) -> list[str]:
        """Brings the settings from before the last change back (and that change becomes the one to take back)."""
        if not self.previous_path.exists():
            raise ConfigError("There is nothing to undo.")
        return self.write(load(self.previous_path))

    async def apply(self) -> None:
        """Puts the settings file just written into effect, without a restart (see reload.py)."""
        if self.reloader is not None:
            await self.reloader.reload()

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
        tr = translator(lang)
        html = self.env.get_template(template).render(
            session=session,
            menu=nav.build(session.role if session else None, self.config.developer, tr),
            can=(lambda path, method="GET": may(session.role, method, path)) if session else (lambda *_: False),
            version=__version__,
            draft=SPEC_DRAFT,
            name=self.config.name,
            developer=self.config.developer,
            reload_error=getattr(self.reloader, "error", None),
            badges=self.badges(session.role) if session else {},
            preset_labels={
                "full": tr("Full"),
                "child": tr("Child"),
                "guest": tr("Guest"),
                "custom": tr("Custom"),
            },
            roles={
                "owner": tr("owner"),
                "admin": tr("administrator"),
                "manager": tr("manager"),
                "observer": tr("read-only"),
            },
            lang=lang,
            theme="light" if request.cookies.get(_THEME_COOKIE) == "light" else "dark",
            mode=request.cookies.get(_THEME_COOKIE)
            if request.cookies.get(_THEME_COOKIE) in ("light", "dark")
            else "auto",
            htmllang=HTML_LANG[lang],
            lang_flag=flag(lang),
            lang_code=lang.split("-")[0].upper()
            if lang in ("en", "fr", "de", "es", "it", "nl", "pt", "ru", "ja", "ko")
            else lang.upper(),
            languages=[(code, NAMES[code], flag(code)) for code in LANGUAGES],
            here=request.url.path if request.url.path.startswith("/admin") else "/admin/",
            _=tr,
            **ctx,
        )
        return HTMLResponse(html, status_code=status, headers=_HEADERS)

    def home(self, request: Request, status: str) -> HTMLResponse:
        """The page at the root, for a person who opens the bridge's address in a browser: no figure, no version,
        nothing the protocol's own routes do not already say. Self-contained, so it needs no file the local-network
        guard would refuse to a stranger; its one style block is allowed by its hash."""
        lang = self.lang(request)
        html = self.env.get_template("home.html").render(
            lang=lang, name=self.config.name, status=status, admin=self.reachable(request), _=translator(lang)
        )
        style = re.search(r"<style>(.*?)</style>", html, re.S)
        digest = base64.b64encode(hashlib.sha256((style.group(1) if style else "").encode()).digest()).decode()
        headers = {
            **_HEADERS,
            "Content-Security-Policy": f"default-src 'none'; style-src 'sha256-{digest}'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'",
        }
        return HTMLResponse(html, headers=headers)

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

    def badges(self, role: str | None = "admin") -> dict[str, dict[str, Any]]:
        """What the menu marks with a dot: where something needs a look, or is going on. Each entry is a count and a
        level (bad: broken, wait: worth a look, info: going on). The Manage button sums what its pages hold."""
        runtime = self.runtime
        found: dict[str, dict[str, Any]] = {}

        def put(key: str, count: int, level: str) -> None:
            if count > 0:
                found[key] = {"n": count, "level": level}

        waiting = len(self.lantern.desk.waiting()) if self.lantern.desk is not None else 0
        look = [i for i in attention.collect(runtime, waiting, self.restart_needed()) if i.level != attention.INFO]
        put("overview", len(look), "bad" if any(i.level == attention.BAD for i in look) else "wait")
        put("activity", len(runtime.traffic.active), "info")
        put("library", len(runtime.browser.catalog().flagged()), "wait")
        put("pairings", waiting, "wait")
        put("sources", sum(1 for state in runtime.stats.sources.values() if state.reachable is False), "bad")
        put(
            "homebrew", sum(1 for row in (runtime.releases.status() if runtime.releases else []) if row["error"]), "bad"
        )
        jobs = runtime.compressor.jobs() if runtime.compressor is not None else []
        failed = sum(1 for j in jobs if j.state == "failed" and time.time() - j.finished < 3600)
        busy = sum(1 for j in jobs if j.state in ("queued", "running"))
        put("tasks", failed or busy, "bad" if failed else "info")
        # Only the pages the role may open carry a dot
        found = {k: v for k, v in found.items() if may(role, "GET", "/admin/" if k == "overview" else f"/admin/{k}")}
        held = [found[k] for k in ("pairings", "sources", "homebrew", "tasks") if k in found]
        put(
            "manage",
            sum(b["n"] for b in held),
            next((lv for lv in ("bad", "wait", "info") if any(b["level"] == lv for b in held)), "info"),
        )
        return found

    def report(self, full: bool = False) -> str:
        """The bridge's state as text to paste in a support request. Without `full` it holds no password, no
        token, no address and no path, and none of the messages of the log (they name files)."""
        import lantern

        state = self.state()
        config, runtime = self.config, self.runtime
        out = [
            f"lantern-bridge {__version__} · Lantern draft {SPEC_DRAFT} · SDK {getattr(lantern, '__version__', '?')}",
            f"Python {platform.python_version()} on {platform.system()} {platform.machine()}"
            f" · up {_duration(time.time() - runtime.stats.started)}",
            "",
            "Access: "
            + ("public (no pairing)" if config.public else "devices pair first")
            + f" · administration {'on the local network only' if config.admin_lan_only else 'open to any address'}"
            + (" · developer mode" if config.developer else ""),
            f"Library: {state['titles']} titles, {state['apps']} apps"
            f" · {state['hashed']} of {state['total']} files hashed",
            f"Processor: {os.cpu_count() or '?'} cores · SHA-256 on {sha_instructions()}"
            f" · {workers(config.hash_threads)} file(s) hashed at once"
            f"{'' if config.hash_threads else ' (automatic)'}"
            f" · conversions on {config.compress_threads or 'every core'}"
            f"{'' if config.compress_threads else ' (automatic)'}",
            "",
            f"Sources ({len(config.sources)}):",
        ]
        by_id = {row["id"]: row for row in state["sources"]}
        for source in config.sources:
            row = by_id.get(source.id, {})
            kind = type(source).__name__.removesuffix("Source").lower()
            seen = (
                "reachable"
                if row.get("reachable")
                else "NOT reachable"
                if row.get("reachable") is False
                else "not read yet"
            )
            where = ""
            if full:
                where = " · " + (
                    re.sub(r"//[^/@]*@", "//", source.url) if isinstance(source, ShopSource) else str(source.path)
                )
            out.append(
                f"- {source.id}: {kind}, {seen}, {row.get('hashed', 0)} hashed, {row.get('pending', 0)} waiting{where}"
            )
            if row.get("error") and full:
                out.append(f"    last error: {row['error']}")
        out += ["", "Integrations:"]
        out.append(
            "- MQTT: "
            + (
                f"on ({config.mqtt.host}:{config.mqtt.port})"
                if full and config.mqtt
                else "on"
                if config.mqtt
                else "off"
            )
        )
        out.append(
            f"- webhook: {'set' if config.webhook else 'off'} · notifications: {'set' if config.notify else 'off'}"
        )
        out.append(
            f"- Atlas: {'on' if config.atlas else 'off'} · update check: {'on' if config.update_check else 'off'}"
            f" · metrics: {'on' if config.metrics else 'off'}"
            f" · operators: {len(self.store.operators())}"
        )
        out += ["", "Health:"]
        for found in health.run(runtime):
            out.append(f"- {found.status}: {found.name}" + (f" — {found.detail}" if full and found.detail else ""))
        problem = getattr(self.reloader, "error", None)
        out.append("")
        out.append(
            "Settings: "
            + ("the last change was NOT applied" + (f": {problem}" if full else "") if problem else "applied")
        )
        warnings = self.ring.since(0, "WARNING", limit=200)
        out.append("")
        out.append(f"Warnings and errors since the start: {len(warnings)}")
        if full:
            for line in warnings[-20:]:
                out.append(f"- {time.strftime('%H:%M:%S', time.localtime(line.time))} {line.level} {line.message}")
        return "\n".join(out) + "\n"

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
            secure=self.secure(request),
        )

    def peer(self, request: Request) -> str | None:
        return request.client.host if request.client else None

    def client(self, request: Request) -> str:
        """The address of the client: behind a trusted proxy, the one it forwards."""
        return netguard.client_address(self.peer(request), request.headers.get("x-forwarded-for"), self.proxies)

    def secure(self, request: Request) -> bool:
        """Whether the browser speaks HTTPS (itself, or through a trusted proxy that ends it)."""
        return netguard.is_https(
            request.url.scheme, self.peer(request), request.headers.get("x-forwarded-proto"), self.proxies
        )

    def own_form(self, request: Request) -> bool:
        """Whether a form without a session (sign-in, preferences) comes from one of our own pages."""
        trusted = netguard.within(self.peer(request), self.proxies)
        return netguard.same_site_request(
            request.headers.get("sec-fetch-site"),
            request.headers.get("origin"),
            request.headers.get("host"),
            request.headers.get("x-forwarded-host") if trusted else None,
        )

    def allowed(self, request: Request) -> bool:
        return not self.config.admin_lan_only or self.reachable(request)

    def reachable(self, request: Request) -> bool:
        """Whether the client is on the local network (or in `admin_allow`), behind a trusted proxy included."""
        address = self.client(request)
        if self.networks:
            return is_loopback(address) or netguard.within(address, self.networks)
        return is_local(address)

    async def form(self, request: Request) -> dict[str, str]:
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > _BODY_LIMIT:
                return {}
        return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}

    def session(self, request: Request) -> Session | None:
        # a block asking again by itself (the replay of a live stream) is not someone using the page
        return self.store.session(request.cookies.get(_COOKIE), touch=not request.scope.get("replay"))


def mount(app: FastAPI, admin: Admin) -> None:
    async def local_only(request: Request) -> None:
        # A dependency of the router, not a middleware: the protocol's own routes (and their streams) are untouched.
        if not admin.allowed(request):
            raise HTTPException(403, "The administration pages answer on the local network only.")
        if not netguard.host_ok(request.headers.get("host"), admin.config.admin_hosts):
            raise HTTPException(400, "The administration pages do not answer to this name.")

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

    @router.get("/sw.js")
    async def worker() -> Response:
        """The service worker, at the root of the pages so that it covers them all (it caches nothing)."""
        return Response(
            (static / "sw.js").read_bytes(),
            media_type="text/javascript",
            headers={**_HEADERS, "Cache-Control": "no-cache"},
        )

    @router.post("/lang")
    async def language(request: Request) -> Response:
        if not admin.own_form(request):
            return Response("The form is out of date: go back and try again.", 403, headers=_HEADERS)
        fields = await admin.form(request)
        target = fields.get("next", "")
        back = target if target.startswith("/admin") and "//" not in target and "\\" not in target else "/admin/"
        response = RedirectResponse(back, status_code=303, headers=_HEADERS)
        if fields.get("lang") in LANGUAGES:
            response.set_cookie(
                LANG_COOKIE,
                fields["lang"],
                max_age=365 * 86400,
                path="/admin",
                samesite="strict",
                httponly=True,
                secure=admin.secure(request),
            )
        return response

    @router.post("/theme")
    async def theme(request: Request) -> Response:
        """Automatic follows the browser's own preference (the page script reads it); light and dark are a choice of
        this browser, kept in a cookie. Without the script the pages are dark."""
        if not admin.own_form(request):
            return Response("The form is out of date: go back and try again.", 403, headers=_HEADERS)
        fields = await admin.form(request)
        target = fields.get("next", "")
        back = target if target.startswith("/admin") and "//" not in target and "\\" not in target else "/admin/"
        response = RedirectResponse(back, status_code=303, headers=_HEADERS)
        if fields.get("theme") in ("light", "dark"):
            response.set_cookie(
                _THEME_COOKIE,
                fields["theme"],
                max_age=365 * 86400,
                path="/admin",
                samesite="strict",
                httponly=True,
                secure=admin.secure(request),
            )
        else:
            response.delete_cookie(_THEME_COOKIE, path="/admin")
        return response

    @router.get("/setup")
    async def setup_form(request: Request) -> Response:
        if admin.store.setup_code() is None:
            return redirect("/login", request)
        return admin.page(request, "setup.html", error=None)

    @router.post("/setup")
    async def setup_post(request: Request) -> Response:
        if not admin.own_form(request):
            return Response("The form is out of date: go back and try again.", 403, headers=_HEADERS)
        fields = await admin.form(request)
        key = admin.client(request)
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
        if not admin.own_form(request):
            return Response("The form is out of date: go back and try again.", 403, headers=_HEADERS)
        fields = await admin.form(request)
        key = admin.client(request)
        who = "name:" + fields.get("name", "").strip().lower()
        if wait := max(admin.attempts.wait(key), admin.accounts.wait(who)):
            return admin.page(
                request,
                "login.html",
                status=429,
                error=admin.tr(request)("Too many tries: wait {wait} s.", wait=f"{wait:.0f}"),
            )
        role = admin.store.identify(fields.get("name", ""), fields.get("password", ""))
        if role is None:
            admin.attempts.record(key)
            admin.accounts.record(who)
            log.warning("a sign-in failed from %s", key)
            if max(admin.attempts.wait(key), admin.accounts.wait(who)):  # this very failure shut the door
                log.warning("sign-ins are blocked for a while (from %s)", key)
                admin.runtime.history.event("signin_blocked", detail="too many wrong passwords")
            return admin.page(request, "login.html", status=401, error=admin.tr(request)("Wrong name or password."))
        token, _ = admin.store.login(
            fields["name"].strip(), role, address=key, agent=request.headers.get("user-agent", "")
        )
        return redirect("/", request, token)

    async def authed(request: Request) -> tuple[Session | None, dict[str, str], Response | None]:
        """The session, the form of a POST, and the response to give instead when the request is refused."""
        if not admin.store.configured:
            return None, {}, redirect("/setup", request)
        session = admin.session(request)
        if session is None:
            return None, {}, redirect("/login", request)
        if not may(session.role, request.method, request.url.path):
            return None, {}, admin.page(request, "observer.html", session, status=403, active="")
        fields: dict[str, str] = {}
        if request.method == "POST":
            fields = await admin.form(request)
            if not secrets.compare_digest(fields.get("csrf", "").encode(), session.csrf.encode()):
                return None, {}, Response("The form is out of date: go back and try again.", 403, headers=_HEADERS)
        return session, fields, None

    @router.get("/sessions")
    async def sessions_page(request: Request) -> Response:
        """The open sessions of the administration pages, with a way to end them."""
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        return admin.page(
            request,
            "sessions.html",
            session,
            active="sessions",
            rows=admin.store.listing(request.cookies.get(_COOKIE)),
            message=admin.tr(request)(_DONE[request.query_params["m"]])
            if request.query_params.get("m") in _DONE
            else None,
        )

    @router.post("/sessions/others/end")
    async def sessions_end_others(request: Request) -> Response:
        _, _, refused = await authed(request)
        if refused is not None:
            return refused
        ended = admin.store.revoke_others(request.cookies.get(_COOKIE))
        if ended:
            log.info("%d other administration session(s) ended from the pages", ended)
            admin.runtime.history.event("session_ended", detail=f"{ended} session(s)")
        return redirect("/sessions?m=ended", request)

    @router.post("/sessions/{sid}/end")
    async def session_end(sid: str, request: Request) -> Response:
        _, _, refused = await authed(request)
        if refused is not None:
            return refused
        if not admin.store.revoke(sid):
            return Response("No such session.", 404, headers=_HEADERS)
        admin.runtime.history.event("session_ended", detail="1 session")
        log.info("an administration session was ended from the pages")
        # Ending your own is signing out: the cookie no longer opens anything, so the next page is the sign-in.
        return redirect("/sessions?m=ended", request)

    # -- operators and one's own account ---------------------------------------

    def operators_page(
        request: Request, session: Session, status: int = 200, error: str | None = None, message: str | None = None
    ) -> Response:
        open_sessions: dict[str, int] = {}
        for row in admin.store.listing():
            open_sessions[row["name"]] = open_sessions.get(row["name"], 0) + 1
        return admin.page(
            request,
            "operators.html",
            session,
            status=status,
            active="operators",
            operators=admin.store.operators(),
            open_sessions=open_sessions,
            role_names=ROLES,
            error=error,
            message=message,
        )

    @router.get("/operators")
    async def operators_get(request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None or session is None:
            return refused or redirect("/login", request)
        key = request.query_params.get("m")
        return operators_page(request, session, message=admin.tr(request)(_DONE[key]) if key in _DONE else None)

    @router.post("/operators")
    async def operators_add(request: Request) -> Response:
        session, fields, refused = await authed(request)
        if refused is not None or session is None:
            return refused or redirect("/login", request)
        name = fields.get("name", "").strip()
        problem = admin.store.add(name, fields.get("role", ""), fields.get("password", ""))
        if problem is not None:
            return operators_page(request, session, 400, error=admin.tr(request)(problem))
        log.info("operator %s added as %s", name, fields.get("role"))
        admin.runtime.history.event("operator_added", name)
        return redirect("/operators?m=operator_added", request)

    @router.post("/operators/role")
    async def operators_role(request: Request) -> Response:
        session, fields, refused = await authed(request)
        if refused is not None or session is None:
            return refused or redirect("/login", request)
        name = fields.get("name", "").strip()
        problem = admin.store.set_role(name, fields.get("role", ""))
        if problem is not None:
            return operators_page(request, session, 400, error=admin.tr(request)(problem))
        log.info("operator %s is now %s", name, fields.get("role"))
        admin.runtime.history.event("operator_changed", name)
        return redirect("/operators?m=operator_changed", request)

    @router.post("/operators/password")
    async def operators_password(request: Request) -> Response:
        session, fields, refused = await authed(request)
        if refused is not None or session is None:
            return refused or redirect("/login", request)
        name = fields.get("name", "").strip()
        keep = request.cookies.get(_COOKIE) if name == session.name else None
        problem = admin.store.set_password(name, fields.get("password", ""), keep)
        if problem is not None:
            return operators_page(request, session, 400, error=admin.tr(request)(problem))
        log.info("the password of operator %s was set by the owner", name)
        admin.runtime.history.event("password_changed", name)
        return redirect("/operators?m=password_changed", request)

    @router.post("/operators/remove")
    async def operators_remove(request: Request) -> Response:
        session, fields, refused = await authed(request)
        if refused is not None or session is None:
            return refused or redirect("/login", request)
        name = fields.get("name", "").strip()
        if name == session.name:
            return operators_page(request, session, 400, error=admin.tr(request)("You cannot remove yourself."))
        problem = admin.store.remove(name)
        if problem is not None:
            return operators_page(request, session, 400, error=admin.tr(request)(problem))
        log.info("operator %s removed", name)
        admin.runtime.history.event("operator_removed", name)
        return redirect("/operators?m=operator_removed", request)

    def me_page(
        request: Request, session: Session, status: int = 200, error: str | None = None, message: str | None = None
    ) -> Response:
        return admin.page(request, "me.html", session, status=status, active="", error=error, message=message)

    @router.get("/me")
    async def me_get(request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None or session is None:
            return refused or redirect("/login", request)
        key = request.query_params.get("m")
        return me_page(request, session, message=admin.tr(request)(_DONE[key]) if key in _DONE else None)

    @router.post("/me")
    async def me_post(request: Request) -> Response:
        session, fields, refused = await authed(request)
        if refused is not None or session is None:
            return refused or redirect("/login", request)
        t = admin.tr(request)
        key, who = admin.client(request), "name:" + session.name.lower()
        if wait := max(admin.attempts.wait(key), admin.accounts.wait(who)):  # a stolen session cannot guess it
            return me_page(request, session, 429, error=t("Too many tries: wait {wait} s.", wait=f"{wait:.0f}"))
        if fields.get("password", "") != fields.get("again", ""):
            return me_page(request, session, 400, error=t("The two new passwords differ."))
        problem = admin.store.change_own_password(
            session.name, fields.get("current", ""), fields.get("password", ""), request.cookies.get(_COOKIE)
        )
        if problem is not None:
            if problem == "The current password is not right.":
                admin.attempts.record(key)
                admin.accounts.record(who)
            return me_page(request, session, 400, error=t(problem))
        log.info("operator %s changed their password", session.name)
        admin.runtime.history.event("password_changed", session.name)
        return redirect("/me?m=password_changed", request)

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
        state = admin.state()
        part = {"todo": "_todo.html", "sources": "_sources.html"}.get(request.query_params.get("fragment", ""))
        return admin.page(
            request,
            part or "overview.html",
            session,
            active="overview",
            checks=health.run(admin.runtime),
            todo=attention.collect(
                admin.runtime,
                state["waiting"],
                admin.restart_needed(),
                paired=len(admin.lantern.accounts.devices()) if admin.lantern.accounts is not None else None,
            ),
            state=state,
            uid=admin.config.uid,
            public=admin.config.public,
            message=admin.tr(request)(_DONE[done]) if (done := request.query_params.get("m", "")) in _DONE else None,
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
        return redirect("/?m=" + ("queued" if fields.get("action") == "rehash" else "scan"), request)

    def cheats_of(catalog: Catalog) -> dict[str, int]:
        """How many cheat sets each title of the library has (the sets of titles it does not list are left out)."""
        found: dict[str, int] = {}
        for cheat in admin.runtime.projection.library.cheats():
            title = cheat.title_id.upper()
            if title in catalog.titles:
                found[title] = found.get(title, 0) + 1
        return found

    def icons_of(title_ids: Iterable[str]) -> set[str]:
        """The titles, among these, whose sheet has an icon the bridge holds."""
        projection = admin.runtime.projection
        return {i for i in title_ids if (sheet := projection.title(i)) is not None and sheet.icon is not None}

    def chosen(request: Request, catalog: Catalog) -> list[TitleInfo] | None:
        """The titles an export or a bulk action is about: those whose ids are in `ids` (comma separated), else
        those the filters of the library page keep, else None (the whole library)."""
        q = request.query_params
        ids = [i.strip().upper() for i in q.get("ids", "").split(",") if i.strip()][:_BULK]
        if ids:
            return [catalog.titles[i] for i in ids if i in catalog.titles]
        if not any(q.get(k) for k in ("q", "type", "source", "state", "genre")):
            return None
        return search(
            catalog,
            flagged=catalog.flagged(),
            q=q.get("q", "")[:100],
            kind=q.get("type", ""),
            source=q.get("source", ""),
            state=q.get("state", ""),
            genre=q.get("genre", ""),
            with_cheats=cheats_of(catalog),
            per_page=10**9,
        ).titles

    @router.get("/library/export")
    async def library_export(request: Request) -> Response:
        """The library as a file to keep: ?format=json (default, with a schema number) or csv. With the library
        page's filters, or `ids`, only those titles."""
        _, _, refused = await authed(request)
        if refused is not None:
            return refused
        catalog = admin.runtime.browser.catalog()
        titles = chosen(request, catalog)
        stamp = time.strftime("%Y-%m-%d", time.gmtime())
        if request.query_params.get("format") == "csv":
            body, kind, extension = catalog.export_csv(titles), "text/csv; charset=utf-8", "csv"
        else:
            document = catalog.export(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), titles)
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
        every = admin.runtime.browser.catalog().issues()
        kind = request.query_params.get("kind", "")
        kept = [i for i in every if i.kind == kind] if kind in KINDS else every
        return admin.page(
            request,
            "checks.html",
            session,
            active="library",
            issues=kept,
            kind=kind if kind in KINDS else "",
            counts={k: sum(1 for i in every if i.kind == k) for k in KINDS},
            total=len(every),
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

    @router.get("/library/find")
    async def library_find(request: Request) -> Response:
        """The titles whose name or id holds the text: what the command box offers beside the pages."""
        _, _, refused = await authed(request)
        if refused is not None:
            return Response(status_code=401, headers=_HEADERS)
        catalog = admin.runtime.browser.catalog()
        found = search(catalog, q=request.query_params.get("q", "")[:100], per_page=8)
        icons = icons_of(t.title_id for t in found.titles)
        rows = [{"id": t.title_id, "name": t.name, "icon": t.title_id in icons} for t in found.titles]
        return Response(json.dumps(rows), media_type="application/json", headers=_HEADERS)

    @router.get("/library")
    async def library_page(request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        q = request.query_params
        catalog = admin.runtime.browser.catalog()
        number = q.get("page", "1")
        flagged = catalog.flagged()
        cheats = cheats_of(catalog)
        found = search(
            catalog,
            flagged=flagged,
            q=q.get("q", "")[:100],
            kind=q.get("type", ""),
            source=q.get("source", ""),
            state=q.get("state", ""),
            genre=q.get("genre", ""),
            with_cheats=cheats,
            sort=q.get("sort", "name"),
            descending={"asc": False, "desc": True}.get(q.get("dir", "")),
            page=int(number) if number.isdigit() else 1,
        )
        sort = q.get("sort", "name") if q.get("sort", "name") in SORTS else "name"
        down = FIRST_DOWN[sort] if q.get("dir") not in ("asc", "desc") else q.get("dir") == "desc"
        done = _DONE.get(q.get("m", ""))
        return admin.page(
            request,
            "library.html",
            session,
            active="library",
            message=admin.tr(request)(done) if done else None,
            order={"sort": sort, "down": down},
            found=found,
            counts={**catalog.counts(), "cheats": len(cheats)},
            cheats=cheats,
            genres=catalog.genres(),
            icons=icons_of(t.title_id for t in found.titles),
            ignored=sum(len(v) for v in admin.runtime.stats.ignored.values()),
            issues=sum(len(v) for v in flagged.values()),
            flagged=flagged.keys(),
            sources=[s.id for s in admin.config.sources],
            f={k: q.get(k, "") for k in ("q", "type", "source", "state", "genre", "sort", "dir", "view")},
        )

    @router.post("/library/rehash")
    async def library_rehash(request: Request) -> Response:
        """Hash again every file of the titles ticked on the library page (`ids`, comma separated)."""
        _, fields, refused = await authed(request)
        if refused is not None:
            return refused
        catalog = admin.runtime.browser.catalog()
        ids = [i.strip().upper() for i in fields.get("ids", "").split(",") if i.strip()][:_BULK]
        files = sorted({(f.source, f.path) for i in ids if i in catalog.titles for f in catalog.titles[i].files})
        count = admin.runtime.rehash(files) if files else 0
        log.info("rehash of %d title(s) requested from the pages: %d file(s)", len(ids), count)
        back = fields.get("next", "")
        stay = back.startswith("/admin/library?") and "//" not in back and "\\" not in back and "\n" not in back
        target = (back if stay else "/admin/library").partition("?")
        query = "&".join(p for p in target[2].split("&") if p and not p.startswith("m="))
        return RedirectResponse(
            f"{target[0]}?{query + '&' if query else ''}m=queued" if files else target[0],
            status_code=303,
            headers=_HEADERS,
        )

    def convert_hints(title: TitleInfo) -> dict[str, dict[str, Any]]:
        """For each file that can be converted, what the result should weigh for each level and mode (or for the
        one way back), so the page can show it while the choice is made."""
        compressor = admin.runtime.compressor
        found: dict[str, dict[str, Any]] = {}
        if compressor is None:
            return found
        for f in title.files:
            target, action = CONVERSIONS.get(f.format, ("", ""))
            if not target:
                continue
            choices = (
                [(lv, block) for lv in (3, 10, 18, 22) for block in (False, True)]
                if action == "compress"
                else [(18, False)]
            )
            table = {}
            for level, block in choices:
                expected, measured = compressor.estimate(f.format, f.size, level, block)
                key = f"{level}:{'block' if block else 'solid'}" if action == "compress" else "-"
                table[key] = {"text": _estimate_text(f.size, expected), "measured": measured}
            default = table["18:solid" if action == "compress" else "-"]
            found[f"{f.source}/{f.path}"] = {"table": table, "default": default}
        return found

    def title_view(request: Request, session: Session | None, title_id: str, done: str | None = None) -> Response:
        title = admin.runtime.browser.catalog().titles.get(title_id.upper())
        if title is None:
            return admin.page(request, "missing.html", session, status=404, active="library", title_id=title_id[:32])
        cheats = [c for c in admin.runtime.projection.library.cheats() if c.title_id.upper() == title.title_id]
        sheet = admin.runtime.projection.title(title.title_id)
        return admin.page(
            request,
            "title.html",
            session,
            active="library",
            title=title,
            issues=admin.runtime.browser.catalog().flagged().get(title.title_id, []),
            sheet=sheet,
            has_image=bool(sheet is not None and sheet.icon is not None),
            shots=len(sheet.screenshots or []) if sheet is not None else 0,
            cheats=cheats,
            timings=admin.runtime.stats.timings,
            open_folders={x.id for x in admin.config.sources if isinstance(x, FolderSource) and x.writable},
            conversions=CONVERSIONS,
            hints=convert_hints(title),
            compress_status=admin.runtime.compressor.status() if admin.runtime.compressor is not None else "no_tool",
            atlas=admin.runtime.atlas.get(title.title_id) if admin.runtime.atlas is not None else None,
            message=admin.tr(request)(_DONE[done]) if done else None,
        )

    @router.get("/library/{title_id}")
    async def title_page(title_id: str, request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        done = request.query_params.get("m")
        return title_view(request, session, title_id, done if done in _DONE else None)

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

    def writable_file(title: TitleInfo, source: str, path: str) -> tuple[Path | None, str | None]:
        """The file on disk behind one file of a title, if the pages may change it; else why not.

        It has to be a file of that title, in a folder the settings allow changes in (the running settings: an
        allowance saved but not applied does not count), and it has to be a regular file inside that folder."""
        if (source, path) not in {(f.source, f.path) for f in title.files}:
            return None, "No such file in this title."
        allowed = next(
            (x for x in admin.config.sources if x.id == source and isinstance(x, FolderSource) and x.writable), None
        )
        root = admin.runtime.roots.get(source)
        if allowed is None or root is None:
            return None, "This folder is not open to changes: allow it in the sources."
        real_root = root.resolve()
        target = root.joinpath(*path.split("/"))
        if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(real_root):
            return None, "The file is not there any more, or is not a plain file of the folder."
        return target, None

    @router.post("/library/{title_id}/delete")
    async def title_delete(title_id: str, request: Request) -> Response:
        """Deletes one file of a title from the disk (only in a folder the settings open to changes)."""
        session, fields, refused = await authed(request)
        if refused is not None:
            return refused
        title = admin.runtime.browser.catalog().titles.get(title_id.upper())
        if title is None:
            return admin.page(request, "missing.html", session, status=404, active="library")
        target, why = writable_file(title, fields.get("source", ""), fields.get("path", ""))
        if target is None:
            return Response(admin.tr(request)(why or ""), 403, headers=_HEADERS)
        try:
            target.unlink()
        except OSError as error:
            log.warning("a file of %s could not be deleted: %s", title.title_id, error.strerror or error)
            return Response(
                admin.tr(request)("The file could not be deleted: the folder may be mounted read-only."),
                500,
                headers=_HEADERS,
            )
        log.info("a file of %s was deleted from the pages (source %s)", title.title_id, fields.get("source", ""))
        admin.runtime.history.event("deleted", subject=title.title_id, detail="1 file")
        admin.runtime.forget_file(fields.get("source", ""), fields.get("path", ""))
        last = len(title.files) == 1  # nothing of the title is left: back to the library
        where = "/admin/library?m=deleted" if last else f"/admin/library/{title.title_id}?m=deleted"
        return RedirectResponse(where, status_code=303, headers=_HEADERS)

    @router.post("/library/{title_id}/convert")
    async def title_convert(title_id: str, request: Request) -> Response:
        """Queues the compression (or the decompression) of one file of a title, in a folder open to changes."""
        session, fields, refused = await authed(request)
        if refused is not None:
            return refused
        t = admin.tr(request)
        title = admin.runtime.browser.catalog().titles.get(title_id.upper())
        if title is None:
            return admin.page(request, "missing.html", session, status=404, active="library")
        target, why = writable_file(title, fields.get("source", ""), fields.get("path", ""))
        if target is None:
            return Response(t(why or ""), 403, headers=_HEADERS)
        compressor = admin.runtime.compressor
        if compressor is None or compressor.status() != "ready":
            return Response(t(_NOT_READY[compressor.status() if compressor else "no_tool"]), 409, headers=_HEADERS)
        level = fields.get("level", "")
        try:
            job = compressor.submit(
                fields["source"],
                fields["path"],
                title.title_id,
                level=int(level) if level.isdigit() else 18,
                block=fields.get("mode") == "block",
                delete_original=fields.get("delete") == "on",
                size=target.stat().st_size,
            )
        except ValueError as error:
            return Response(t(str(error)), 400, headers=_HEADERS)
        log.info("a conversion of %s was queued from the pages (job %d, %s)", title.title_id, job.id, job.action)
        return RedirectResponse("/admin/tasks?m=converting", status_code=303, headers=_HEADERS)

    def tasks_data(request: Request) -> dict[str, Any]:
        compressor = admin.runtime.compressor
        done = _DONE.get(request.query_params.get("m", ""))
        return {
            "jobs": compressor.jobs() if compressor is not None else [],
            "compress_status": compressor.status() if compressor is not None else "no_tool",
            "message": admin.tr(request)(done) if done else None,
            "now": time.time(),
        }

    @router.get("/tasks")
    async def tasks(request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        template = "_tasks.html" if request.query_params.get("fragment") else "tasks.html"
        return admin.page(request, template, session, active="tasks", **tasks_data(request))

    @router.post("/tasks/{job_id}/cancel")
    async def task_cancel(job_id: int, request: Request) -> Response:
        _, _, refused = await authed(request)
        if refused is not None:
            return refused
        compressor = admin.runtime.compressor
        if compressor is None or not compressor.cancel(job_id):
            return Response("No such running task.", 404, headers=_HEADERS)
        return RedirectResponse("/admin/tasks", status_code=303, headers=_HEADERS)

    def activity_data() -> dict[str, Any]:
        traffic = admin.runtime.traffic
        transfers, clients, _ = traffic.snapshot()
        now = time.monotonic()
        days = admin.runtime.history.days(14)
        return {
            "changes": list(admin.runtime.stats.changes)[:30],
            "days": days,
            "days_max": max((d.sent for d in days), default=0),
            "transfers": transfers,
            "jobs": [
                j
                for j in (admin.runtime.compressor.jobs() if admin.runtime.compressor else [])
                if j.state in ("queued", "running")
            ],
            "clients": clients,
            "rates": traffic.rates(),
            "day": traffic.day(),
            "seconds": traffic.seconds(),
            "totals": traffic.totals,
            "uptime": time.time() - admin.runtime.stats.started,
            "state": admin.state(),
            "now": now,
        }

    @router.get("/events")
    async def events_page(request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        kind = request.query_params.get("kind", "")
        kind = kind if kind in _JOURNAL else ""
        return admin.page(
            request,
            "_events.html" if request.query_params.get("fragment") else "events.html",
            session,
            active="events",
            events=admin.runtime.history.events(200, kind),
            kinds=_JOURNAL,
            kind=kind,
            usable=admin.runtime.history.usable,
        )

    @router.get("/events/export")
    async def events_export(request: Request) -> Response:
        _, _, refused = await authed(request)
        if refused is not None:
            return refused
        kind = request.query_params.get("kind", "")
        kind = kind if kind in _JOURNAL else ""
        rows = admin.runtime.history.events(MAX_EVENTS, kind)
        if request.query_params.get("format") == "json":
            body = json.dumps(
                {
                    "schema": 1,
                    "events": [
                        {"ts": iso_time(e.ts), "kind": e.kind, "subject": e.subject, "detail": e.detail} for e in rows
                    ],
                },
                indent=2,
            )
            return Response(body, media_type="application/json", headers=_attachment("json"))
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(("time", "kind", "subject", "detail"))
        for e in rows:
            writer.writerow([iso_time(e.ts), *(_csv_safe(v) for v in (e.kind, e.subject, e.detail))])
        return Response(out.getvalue(), media_type="text/csv; charset=utf-8", headers=_attachment("csv"))

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

    @router.get("/developer/check")
    async def developer_check(request: Request) -> Response:
        """What a console would meet asking for one title (see selfcheck.py)."""
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        developer_only()
        title_id = request.query_params.get("id", "").strip().upper()
        if not re.fullmatch(r"[0-9A-F]{16}", title_id):
            raise HTTPException(404)
        return admin.page(
            request,
            "check.html",
            session,
            active="developer",
            title_id=title_id,
            steps=selfcheck.check(admin.runtime, title_id),
        )

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

    def ensure_account(accounts: Any, name: str, preset: str) -> Any:
        """The account of that name, made with the rights of the preset when it does not exist (a child's account
        takes the usual age limit); None for a preset the pages do not know. An existing account keeps its own."""
        found = accounts.user(name)
        if found is not None:
            return found
        if preset not in presets.PRESETS:
            return None
        user = accounts.create_user(name, presets.PRESETS[preset])
        if preset == "child":
            user = accounts.update_user(name, rating_cap=presets.cap_from("child", "", False)[0])
        admin.runtime.history.event("account_added", name)
        return user

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
            admin.runtime.history.event("pairing_refused")
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
        user = ensure_account(accounts, name, fields.get("preset", "full"))
        if user is None:
            return admin.page(
                request,
                "pairings.html",
                session,
                status=400,
                active="pairings",
                waiting=desk.waiting(),
                users=[u.name for u in accounts.users()],
                error=admin.tr(request)("Choose a preset from the list."),
            )
        granted = desk.approve(entry, user.name)
        log.info("pairing of %s approved for %s: %s", entry.device, user.name, ", ".join(granted) or "no scope")
        admin.runtime.history.event("paired", user.name)
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
            admin.runtime.history.event("revoked")
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

    @router.get("/api/badges")
    async def api_badges(request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None or session is None:
            return Response(status_code=401, headers=_HEADERS)
        return Response(json.dumps(admin.badges(session.role)), media_type="application/json", headers=_HEADERS)

    @router.get("/live")
    async def live(request: Request) -> Response:
        """Server-sent events for one block of a page: its fresh content, sent when it changes (not every pass)."""
        _, _, refused = await authed(request)
        if refused is not None:
            return Response(status_code=401, headers=_HEADERS)
        target = request.query_params.get("u", "")
        if target.partition("?")[0] not in _FOLLOWED or "\n" in target or "\r" in target:
            raise HTTPException(404)

        holder = request.cookies.get(_COOKIE, "")
        if admin.streams.get(holder, 0) >= _STREAMS_PER_SESSION:
            return Response("Too many open streams for this session.", status_code=429, headers=_HEADERS)
        admin.streams[holder] = admin.streams.get(holder, 0) + 1

        async def events() -> AsyncIterator[str]:
            last: str | None = None
            sent = time.monotonic()
            try:
                while not await request.is_disconnected():
                    if admin.reloader is not None and admin.reloader.current is not request.app:
                        return  # the settings were applied: the browser reconnects to the new server
                    runtime = admin.runtime
                    with runtime.pulse.listen() as changed:  # opened first: a change while rendering is not lost
                        body = await _ask(request, target)
                        if (
                            body is None
                        ):  # the session ended or the page failed: the browser reconnects, is sent to sign in
                            return
                        if body != last:
                            last = body
                            yield f"data: {json.dumps(body)}\n\n"
                            sent = time.monotonic()
                        elif time.monotonic() - sent >= _BEAT:
                            yield ": keep-alive\n\n"
                            sent = time.monotonic()
                        # What changes by itself, the rates and the durations, is looked at every second while
                        # something is going on; otherwise the page waits for a change, or a few seconds.
                        moving = bool(runtime.traffic.active) or any(runtime.traffic.rates().values())
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(changed.wait(), _BUSY if moving else _IDLE)
                    await asyncio.sleep(_SETTLE)  # a burst of changes is one look
            finally:
                left = admin.streams.get(holder, 1) - 1
                if left > 0:
                    admin.streams[holder] = left
                else:
                    admin.streams.pop(holder, None)

        return StreamingResponse(events(), media_type="text/event-stream", headers=_HEADERS)

    # -- settings --------------------------------------------------------------

    def settings_page(
        request: Request,
        session: Session | None,
        status: int = 200,
        *,
        tested: dict[str, Any] | None = None,
        error: str | None = None,
        draft: dict[str, str] | None = None,
        preview: list[str] | None = None,
        imported: str = "",
    ) -> Response:
        t = admin.tr(request)
        sources_view = request.url.path.startswith(("/admin/sources", "/admin/config/sources"))
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
                prefill={},
                can_undo=False,
                atlas_info=None,
                updates=None,
                preview=None,
                imported="",
            )
        sources = [
            {
                "id": x.id,
                "kind": type(x).__name__.removesuffix("Source").lower(),
                "location": x.location,
                "writable": getattr(x, "writable", None),
                "user": getattr(x, "user", None),
                "password_env": getattr(x, "password_env", None),
                "password_set": bool(getattr(x, "password_env", None)) and getattr(x, "password_env", "") in os.environ,
                "hauth_env": getattr(x, "hauth_env", None),
                "hauth_set": bool(getattr(x, "hauth_env", None)) and getattr(x, "hauth_env", "") in os.environ,
            }
            for x in file.sources
        ]
        done = _DONE.get(request.query_params.get("m", ""))
        return admin.page(
            request,
            "sources.html" if sources_view else "config.html",
            session,
            status=status,
            active="sources" if sources_view else "config",
            file=file,
            sources=sources,
            restart=admin.restart_needed(),
            message=t(done) if done else None,
            error=error,
            tested=tested,
            prefill=draft or {},
            can_undo=admin.previous_path.exists(),
            atlas_info=admin.runtime.atlas,
            updates=admin.runtime.updates,
            preview=preview,
            imported=imported,
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
            done_keys: list[str] = []
            if problem is None:
                done_keys = admin.write(config)
        except ConfigError as error:
            problem = str(error)
        except OSError as error:
            problem = f"{error.strerror or error}"
        if problem is not None:
            typed = {k: v for k, v in fields.items() if k != "csrf"}
            said = t(problem) if problem in _FIXED or problem in releases.FIXED else problem
            if fields.get("back") == "homebrew":
                return homebrew_view(request, session, 400, error=said)
            return settings_page(request, session, 400, error=said, draft=typed)
        log.info("settings changed from the pages: %s", done)
        admin.audit(session, done_keys, done)
        await admin.apply()
        back = fields.get("back")
        return redirect(f"/{back if back in ('start', 'sources', 'homebrew') else 'config'}?m={done}", request)

    _FIXED = {
        "No such source.",
        "Only an AeroFoil source has logins.",
        "Choose a number of devices from 1 to 20.",
        "There is nothing to undo.",
        "That repository is already there.",
        "Choose a kind from the list.",
        "That repository is not there.",
    }

    @router.get("/start")
    async def start(request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        t = admin.tr(request)
        done = _DONE.get(request.query_params.get("m", ""))
        accounts = admin.lantern.accounts
        try:
            file = admin.file()
        except ConfigError:
            file = admin.config
        steps = attention.guide(
            admin.runtime,
            admin.state(),
            paired=len(accounts.devices()) if accounts is not None else 0,
            uid=admin.config.uid,
            saved=[x.id for x in file.sources],
            restart=admin.restart_needed(),
        )
        return admin.page(
            request,
            "_start.html" if request.query_params.get("fragment") else "start.html",
            session,
            active="overview",
            steps=steps,
            finished=all(step["done"] for step in steps),
            sources=file.sources,
            message=t(done) if done else None,
        )

    @router.get("/config")
    async def config_page(request: Request) -> Response:
        session, _, refused = await authed(request)
        return refused if refused is not None else settings_page(request, session)

    @router.get("/sources")
    async def sources_page(request: Request) -> Response:
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
            config.metrics = "metrics" in f
            config.update_check = "update_check" in f
            try:
                config.low_disk_mb = int(f.get("low_disk_mb", "500") or 500)
            except ValueError as error:
                raise ConfigError("the disk limit is a number of megabytes") from error
            try:
                config.hash_threads = int(f.get("hash_threads", "0") or 0)
                config.compress_threads = int(f.get("compress_threads", "0") or 0)
            except ValueError as error:
                raise ConfigError("the threads are a number, 0 for automatic") from error
            config.webhook = parse_webhook(f.get("webhook", "").strip())
            config.notify = parse_webhook(f.get("notify", "").strip(), "notify")
            return None

        return await change(request, edit, "saved")

    @router.get("/config/export")
    async def config_export(request: Request) -> Response:
        """The settings as one file (the file itself holds no password: sources name the variable that does)."""
        _, _, refused = await authed(request)
        if refused is not None:
            return refused
        try:
            text = admin.file().to_toml()
        except ConfigError as error:
            log.warning("the settings could not be exported: %s", error)
            return Response("The settings file cannot be read.", 500, headers=_HEADERS)
        return Response(
            text,
            media_type="application/toml",
            headers={**_HEADERS, "Content-Disposition": 'attachment; filename="lantern-bridge-settings.toml"'},
        )

    @router.post("/config/import")
    async def config_import(request: Request) -> Response:
        """Settings from an exported file: shown as a list of what would change, applied on a second step.

        The bridge keeps its own identity (the `uid` the devices are paired with), whatever the file says.
        """
        session, fields, refused = await authed(request)
        if refused is not None:
            return refused
        t = admin.tr(request)
        text = fields.get("toml", "")
        try:
            current = admin.file()
            incoming = check(parse_text(text, current.uid))
            lines = changes(current, incoming)
        except ConfigError as error:
            return settings_page(request, session, 400, error=f"{t('The file cannot be used:')} {error}")
        if not lines:
            return settings_page(request, session, 200, error=t("This file changes nothing."))
        if not fields.get("confirm"):
            return settings_page(request, session, 200, preview=lines, imported=text)
        try:
            done = admin.write(incoming)
        except ConfigError as error:
            return settings_page(request, session, 400, error=str(error))
        except OSError as error:
            return settings_page(request, session, 500, error=f"{error.strerror or error}")
        log.info("settings imported from a file")
        admin.audit(session, done, "imported")
        await admin.apply()
        return redirect("/config?m=imported", request)

    @router.post("/config/backup")
    async def config_backup(request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        scratch = tempfile.NamedTemporaryFile(prefix="lantern-bridge-", suffix=".zip", delete=False)  # noqa: SIM115
        scratch.close()
        try:
            await asyncio.to_thread(backup.make, admin.runtime.state_dir, admin.config_path, Path(scratch.name))
        except (backup.BackupError, ConfigError, OSError, sqlite3.Error) as error:
            with contextlib.suppress(FileNotFoundError):
                os.remove(scratch.name)
            return settings_page(request, session, 400, error=str(error))
        log.info("a backup was downloaded")
        name = f"lantern-bridge-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        return FileResponse(
            scratch.name,
            media_type="application/zip",
            filename=name,
            headers=_HEADERS,
            background=BackgroundTask(Path(scratch.name).unlink, missing_ok=True),
        )

    @router.post("/config/mqtt")
    async def config_mqtt(request: Request) -> Response:
        def edit(config: Config, f: dict[str, str]) -> str | None:
            host = f.get("host", "").strip()
            if not host:
                config.mqtt = None  # no host: nothing is published
                return None
            try:
                port = int(f.get("port", "1883") or 1883)
            except ValueError as error:
                raise ConfigError("the port is a number") from error
            user, env = f.get("user", "").strip() or None, f.get("password_env", "").strip() or None
            if env is not None and not _ENV_NAME.match(env):
                raise ConfigError("a variable's name is letters, digits and _")
            old = config.mqtt
            config.mqtt = parse_mqtt(
                {
                    **(old.extra if old else {}),
                    "host": host,
                    "port": port,
                    "prefix": f.get("prefix", "").strip() or "lantern-bridge",
                    "tls": "tls" in f,
                    **({"user": user} if user else {}),
                    **({"password_env": env} if env else {}),
                }
            )
            return None

        return await change(request, edit, "saved")

    def build(f: dict[str, str]) -> Source:
        """The source the add form describes (ConfigError for what it cannot be)."""
        kind, sid, where = f.get("kind", ""), f.get("id", "").strip(), f.get("where", "").strip()
        cls = _KINDS.get(kind)
        if cls is None:
            raise ConfigError("this kind of source is not supported")
        if kind == "folder":
            return FolderSource(id=sid, path=Path(where), writable=f.get("writable") == "on")
        user, env = f.get("user", "").strip() or None, f.get("password_env", "").strip() or None
        if env is not None and not _ENV_NAME.match(env):
            raise ConfigError("a variable's name is letters, digits and _")
        hauth = f.get("hauth_env", "").strip() or None
        if hauth is not None and not _ENV_NAME.match(hauth):
            raise ConfigError("a variable's name is letters, digits and _")
        built: Source = cls(id=sid, url=where, user=user, password_env=env, hauth_env=hauth)
        return built

    @router.post("/config/sources")
    async def config_add(request: Request) -> Response:
        def edit(config: Config, f: dict[str, str]) -> str | None:
            config.sources.append(build(f))
            return None

        return await change(request, edit, "added")

    @router.post("/config/sources/probe")
    async def config_probe(request: Request) -> Response:
        """Tries the source the form describes, without saving anything."""
        session, fields, refused = await authed(request)
        if refused is not None:
            return refused
        t = admin.tr(request)
        typed = {k: v for k, v in fields.items() if k != "csrf"}
        try:
            source = build(fields)
        except ConfigError as error:
            return settings_page(request, session, 400, error=t(str(error)), draft=typed)
        ok, text = await probe_source(source, t)
        shown = {"id": source.id or t("New source"), "ok": ok, "text": text}
        return settings_page(request, session, tested=shown, draft=typed)

    @router.post("/config/undo")
    async def config_undo(request: Request) -> Response:
        session, fields, refused = await authed(request)
        if refused is not None:
            return refused
        t = admin.tr(request)
        try:
            done_keys = admin.undo()
        except ConfigError as error:
            return settings_page(request, session, 400, error=t(str(error)))
        except OSError as error:
            return settings_page(request, session, 500, error=f"{error.strerror or error}")
        log.info("settings changed from the pages: the last change was taken back")
        admin.audit(session, done_keys, "undone")
        await admin.apply()
        return redirect(f"/{'sources' if fields.get('back') == 'sources' else 'config'}?m=undone", request)

    def find(config: Config, sid: str) -> int | None:
        return next((n for n, x in enumerate(config.sources) if x.id == sid), None)

    def homebrew_view(
        request: Request, session: Session | None, status: int = 200, *, error: str | None = None
    ) -> Response:
        """The GitHub repositories whose releases are served as homebrew, and what became of each."""
        t = admin.tr(request)
        done = _DONE.get(request.query_params.get("m", ""))
        status_of = {row["repo"]: row for row in admin.runtime.releases.status()} if admin.runtime.releases else {}
        try:
            file = admin.file()
        except ConfigError:
            file = admin.config
        rows = []
        for r in file.homebrew:
            row = {
                "repo": r.repo,
                "asset": r.asset,
                "kind": r.kind,
                "prerelease": r.prerelease,
                **status_of.get(r.repo, {}),
            }
            if row.get("error") in releases.FIXED:
                row["error"] = t(row["error"])
            rows.append(row)
        return admin.page(
            request,
            "homebrew.html",
            session,
            status=status,
            active="homebrew",
            rows=rows,
            kinds=releases.KINDS,
            token_set=bool(file.github_token_env and file.github_token_env in os.environ),
            message=t(done) if done else None,
            error=error,
        )

    @router.get("/homebrew")
    async def homebrew_page(request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        return homebrew_view(request, session)

    @router.post("/homebrew/add")
    async def homebrew_add(request: Request) -> Response:
        def edit(config: Config, f: dict[str, str]) -> str | None:
            try:
                name = releases.parse_repo(f.get("repo", ""))
            except releases.ReleaseError as error:
                return str(error)
            if any(r.repo.lower() == name.lower() for r in config.homebrew):
                return "That repository is already there."
            kind = f.get("kind", "") or None
            if kind is not None and kind not in releases.KINDS:
                return "Choose a kind from the list."
            asset = f.get("asset", "").strip() or None
            config.homebrew.append(HomebrewRepo(name, asset, kind, "prerelease" in f))
            return None

        return await change(request, edit, "homebrew_added")

    @router.post("/homebrew/remove")
    async def homebrew_remove(request: Request) -> Response:
        def edit(config: Config, f: dict[str, str]) -> str | None:
            keep = [r for r in config.homebrew if r.repo != f.get("repo", "")]
            if len(keep) == len(config.homebrew):
                return "That repository is not there."
            config.homebrew = keep
            return None

        return await change(request, edit, "homebrew_removed")

    @router.post("/homebrew/check")
    async def homebrew_check(request: Request) -> Response:
        _, fields, refused = await authed(request)
        if refused is not None:
            return refused
        if admin.runtime.releases is not None:
            admin.runtime.releases.recheck(fields.get("repo") or None)
        return redirect("/homebrew?m=homebrew_checking", request)

    @router.post("/config/sources/{sid}/remove")
    async def config_remove(sid: str, request: Request) -> Response:
        def edit(config: Config, f: dict[str, str]) -> str | None:
            at = find(config, sid)
            if at is None:
                return "No such source."
            del config.sources[at]
            return None

        return await change(request, edit, "removed")

    @router.post("/config/sources/{sid}/writable")
    async def config_writable(sid: str, request: Request) -> Response:
        """Allows, or forbids, the pages to delete and compress files in a folder."""

        def edit(config: Config, f: dict[str, str]) -> str | None:
            at = find(config, sid)
            folder = config.sources[at] if at is not None else None
            if at is None or not isinstance(folder, FolderSource):
                return "No such source."
            config.sources[at] = dataclasses.replace(folder, writable=f.get("allow") == "1")
            return None

        return await change(request, edit, "saved")

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
        ok, text = await probe_source(source, t)
        return settings_page(request, session, tested={"id": sid, "ok": ok, "text": text})

    async def probe_source(source: Source, t: Any) -> tuple[bool, str]:
        """Whether a source can be read: a folder is looked at, a shop is asked for its listing."""
        if isinstance(source, FolderSource):
            ok = source.path.is_dir() and os.access(source.path, os.R_OK | os.X_OK)
            return ok, t("The folder is there and can be read.") if ok else t("The folder cannot be read.")
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

        return await asyncio.to_thread(probe)

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
        # An AeroFoil login holds the name of a secret: only the roles that may tie one see it
        aero = admin.aerofoil(file) if session and may(session.role, "POST", "/admin/accounts/x/login") else None
        logins = {x.account: x for x in aero.logins} if aero else {}
        rows = []
        for user in accounts.users() if accounts is not None else []:
            login = logins.get(user.name)
            rows.append(
                {
                    "name": user.name,
                    "state": user.state,
                    "scopes": list(user.scopes),
                    "preset": presets.preset_of(user.scopes),
                    "age": user.rating_cap.age if user.rating_cap is not None else None,
                    "hide_unrated": user.rating_cap is not None and user.rating_cap.unrated == "hide",
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
            values={"preset": "full", "scopes": presets.PRESETS["full"], "age": "", "hide_unrated": False},
            choices=presets.grantable(session.role if session else None),
            aero={"id": aero.id, "user": aero.user} if aero else None,
            restart=admin.restart_needed(),
            message=t(done) if done else None,
            error=error,
        )

    @router.get("/accounts")
    async def accounts_get(request: Request) -> Response:
        session, _, refused = await authed(request)
        return refused if refused is not None else accounts_page(request, session)

    # -- the accounts of the players -----------------------------------------------

    def account_values(user: Any) -> dict[str, Any]:
        cap = user.rating_cap
        return {
            "preset": presets.preset_of(user.scopes),
            "scopes": user.scopes,
            "age": "" if cap is None or cap.age is None else str(cap.age),
            "hide_unrated": cap is not None and cap.unrated == "hide",
        }

    def account_view(
        request: Request,
        session: Session,
        name: str,
        *,
        status: int = 200,
        error: str | None = None,
        values: dict[str, Any] | None = None,
    ) -> Response:
        accounts = admin.lantern.accounts
        user = accounts.user(name) if accounts is not None else None
        if accounts is None or user is None:
            return Response("No such account.", 404, headers=_HEADERS)
        done = _DONE.get(request.query_params.get("m", ""))
        return admin.page(
            request,
            "account.html",
            session,
            status=status,
            active="accounts",
            account=user,
            devices=accounts.devices(name),
            invitations=[i for i in accounts.invitations() if i.user == name],
            values=values or account_values(user),
            choices=presets.grantable(session.role),
            editable=not presets.holds_more_than(user.scopes, session.role),
            message=admin.tr(request)(done) if done else None,
            error=error,
        )

    def described(fields: dict[str, str], role: str) -> tuple[tuple[str, ...], Any, str | None]:
        """The scopes and the rating cap a form asks for, and the fixed text of what is wrong with it."""
        preset = fields.get("preset", "")
        scopes, problem = presets.scopes_from(preset, fields, role)
        if problem is not None:
            return (), None, problem
        cap, problem = presets.cap_from(preset, fields.get("age", ""), bool(fields.get("hide_unrated")))
        return scopes, cap, problem

    @router.post("/accounts")
    async def account_create(request: Request) -> Response:
        session, fields, refused = await authed(request)
        if refused is not None or session is None:
            return refused or redirect("/login", request)
        t = admin.tr(request)
        accounts = admin.lantern.accounts
        if accounts is None:
            return redirect("/", request)
        name = fields.get("name", "").strip()
        scopes, cap, problem = described(fields, session.role)
        if not name or len(name) > 64 or not set(name) <= _USER_CHARS:
            problem = "An account name is letters, digits, - _ and . (64 at most)."
        elif accounts.user(name) is not None:
            problem = "An account has that name already."
        if problem is not None:
            return accounts_page(request, session, 400, t(problem))
        try:
            accounts.create_user(name, scopes)
            if cap is not None:
                accounts.update_user(name, rating_cap=cap)
        except AccountError as error:
            return accounts_page(request, session, 400, error.detail)
        log.info("account %s added from the pages", name)
        admin.runtime.history.event("account_added", name)
        return redirect(f"/accounts/{name}?m=account_added", request)

    @router.get("/accounts/{account}")
    async def account_get(account: str, request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None or session is None:
            return refused or redirect("/login", request)
        return account_view(request, session, account)

    @router.post("/accounts/{account}/edit")
    async def account_edit(account: str, request: Request) -> Response:
        session, fields, refused = await authed(request)
        if refused is not None or session is None:
            return refused or redirect("/login", request)
        t = admin.tr(request)
        accounts = admin.lantern.accounts
        user = accounts.user(account) if accounts is not None else None
        if accounts is None or user is None:
            return Response("No such account.", 404, headers=_HEADERS)
        if presets.holds_more_than(user.scopes, session.role):
            return account_view(
                request,
                session,
                account,
                status=403,
                error=t("This account holds a scope that only an administrator may change."),
            )
        scopes, cap, problem = described(fields, session.role)
        if problem is not None:
            return account_view(
                request,
                session,
                account,
                status=400,
                error=t(problem),
                values={
                    "preset": fields.get("preset", "full"),
                    "scopes": tuple(s for s in presets.grantable(session.role) if fields.get(f"scope.{s}")),
                    "age": fields.get("age", ""),
                    "hide_unrated": bool(fields.get("hide_unrated")),
                },
            )
        accounts.update_user(account, scopes=scopes, rating_cap=cap)
        log.info("the rights of account %s were changed from the pages", account)
        admin.runtime.history.event("account_changed", account)
        return redirect(f"/accounts/{account}?m=account_changed", request)

    @router.post("/accounts/{account}/state")
    async def account_state(account: str, request: Request) -> Response:
        session, fields, refused = await authed(request)
        if refused is not None or session is None:
            return refused or redirect("/login", request)
        accounts = admin.lantern.accounts
        state = fields.get("state", "")
        if accounts is None or accounts.user(account) is None:
            return Response("No such account.", 404, headers=_HEADERS)
        if state not in ("active", "frozen"):
            return Response("Choose active or frozen.", 400, headers=_HEADERS)
        accounts.update_user(account, state=state)
        log.info("account %s is now %s", account, state)
        admin.runtime.history.event("account_changed", f"{account}: {state}")
        return redirect(f"/accounts/{account}?m=account_changed", request)

    @router.post("/accounts/{account}/delete")
    async def account_delete(account: str, request: Request) -> Response:
        session, _, refused = await authed(request)
        if refused is not None or session is None:
            return refused or redirect("/login", request)
        t = admin.tr(request)
        accounts = admin.lantern.accounts
        user = accounts.user(account) if accounts is not None else None
        if accounts is None or user is None:
            return Response("No such account.", 404, headers=_HEADERS)
        if presets.holds_more_than(user.scopes, session.role):
            return account_view(
                request,
                session,
                account,
                status=403,
                error=t("This account holds a scope that only an administrator may change."),
            )
        try:
            admin.lantern.delete_account(account)
        except AccountError as error:
            return account_view(request, session, account, status=409, error=error.detail)
        await drop_login(session, account)
        log.info("account %s was deleted from the pages", account)
        admin.runtime.history.event("account_deleted", account)
        return redirect("/accounts?m=account_deleted", request)

    async def drop_login(session: Session, account: str) -> None:
        """An AeroFoil login tied to a deleted account goes with it, so that no name of a secret stays behind."""
        try:
            config = admin.file()
            aero = admin.aerofoil(config)
            if aero is None or not any(x.account == account for x in aero.logins):
                return
            kept = tuple(x for x in aero.logins if x.account != account)
            config.sources[config.sources.index(aero)] = dataclasses.replace(aero, logins=kept)
            admin.audit(session, admin.write(config), "login removed with its account")
            await admin.apply()
        except (ConfigError, OSError) as error:
            log.warning("the AeroFoil login of the deleted account %s could not be removed: %s", account, error)

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
        user = ensure_account(accounts, name, fields.get("preset", "full"))
        if user is None:
            return invitations_page(request, session, 400, t("Choose a preset from the list."))
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

    @router.get("/diagnostic")
    async def diagnostic_page(request: Request) -> Response:
        """The state of the bridge as text, to paste in a support request."""
        session, _, refused = await authed(request)
        if refused is not None:
            return refused
        full = request.query_params.get("full") == "1"
        return admin.page(
            request, "diagnostic.html", session, active="diagnostic", report=admin.report(full), full=full
        )

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
