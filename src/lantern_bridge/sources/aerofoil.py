# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""An AeroFoil as a source: a Tinfoil shop that also keeps saves, cheats and requests.

AeroFoil answers its shop index like any Tinfoil shop (``Shop``); the CyberFoil functions are plain HTTP
endpoints under ``/api``, called here with the login of a user: the service account of the source, or the
one a Lantern account is tied to.

A stock AeroFoil takes that login as HTTP Basic for the saves and the cheat files only; its cheat list, its
cheat writes and its requests want a web session. For those the login is made the way the web page does it
(the ``/login`` form, with the same user and password) and the session cookie is kept, per user.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from lantern_bridge.sources.tinfoil import Shop, UpstreamError

#: The largest answer read from a call (a save archive is read whole, to be hashed).
MAX_CALL = 64 << 20
MAX_LIST = 8 << 20
MAX_CHEATS = 5000
SAVE_TYPES = ("account", "device", "unknown")


@dataclass(frozen=True, slots=True)
class UpstreamSave:
    """A backup AeroFoil keeps."""

    title_id: str
    save_id: str
    save_type: str
    size: int
    created: int  # unix time
    note: str


@dataclass(frozen=True, slots=True)
class UpstreamCheat:
    title_id: str
    build_id: str
    size: int
    note: str


@dataclass(frozen=True, slots=True)
class UpstreamRequest:
    id: int
    title_id: str
    name: str | None
    status: str
    created: int


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _wants_session(method: str, path: str) -> bool:
    """Whether a stock AeroFoil answers this call only to a web session, not to a Basic login."""
    route = path.split("?", maxsplit=1)[0]
    return (
        route.startswith("/api/requests")
        or route == "/api/cheats"
        or (route.startswith("/api/cheats/") and method == "DELETE")
    )


class _SessionEnded(Exception):
    """The AeroFoil sent a call with a session cookie to its login page."""


class AeroFoil(Shop):
    """One AeroFoil, read with one user's login."""

    def as_user(self, user: str | None, password: str | None) -> AeroFoil:
        """The same AeroFoil, called as another user (the connection is shared)."""
        return AeroFoil(self.url, user=user, password=password, client=self._client, hauth=self.hauth)

    def __init__(self, url: str, *, user: str | None = None, password: str | None = None, **kw: Any) -> None:
        super().__init__(url, user=user, password=password, **kw)
        self._login_form = {"user": user, "password": password} if user is not None and password is not None else None
        self._session: str | None = None

    def _sign_in(self) -> str:
        """Sign in as the web page does and return the session cookie; anything else is an error."""
        if self._login_form is None:
            raise UpstreamError("the AeroFoil needs a login for this call")
        try:
            reply = self._client.post(
                self._root() + "/login", data=self._login_form, headers=self.headers(Accept="text/html, */*")
            )
        except httpx.HTTPError as error:
            raise UpstreamError(f"the AeroFoil cannot be reached ({type(error).__name__})") from error
        cookie = reply.cookies.get("session")
        self._client.cookies.clear()  # the connection is shared: a session belongs to its user, not to the jar
        # A refused login is sent back to the login page; an accepted one is sent on, with a session.
        if reply.status_code not in (302, 303) or not cookie or reply.headers.get("location", "").endswith("/login"):
            raise UpstreamError("the AeroFoil refused the login")
        self._session = cookie
        return cookie

    def _root(self) -> str:
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}"

    def call(
        self,
        method: str,
        path: str,
        *,
        limit: int = MAX_LIST,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        document: object = None,
    ) -> bytes:
        """The body of the answer to `method path` of the AeroFoil (a redirect is an error: the API does not
        send any). Raises UpstreamError for anything but a 200."""
        if _wants_session(method, path):
            return self._call_in_session(method, path, limit=limit, data=data, files=files, document=document)
        return self._send(method, path, limit=limit, data=data, files=files, document=document)

    def _call_in_session(self, method: str, path: str, **kw: Any) -> bytes:
        """The same call with the session cookie; the session is made first, and made again once if it ended."""
        try:
            return self._send(method, path, cookie=self._session or self._sign_in(), **kw)
        except _SessionEnded:
            self._session = None
        try:
            return self._send(method, path, cookie=self._sign_in(), **kw)
        except _SessionEnded:
            self._session = None
            raise UpstreamError("the AeroFoil did not keep the session") from None

    def _send(
        self,
        method: str,
        path: str,
        *,
        limit: int,
        data: dict[str, str] | None,
        files: dict[str, tuple[str, bytes, str]] | None,
        document: object,
        cookie: str | None = None,
    ) -> bytes:
        headers = self.headers(Accept="application/json, */*")
        if cookie is not None:
            headers["Cookie"] = f"session={cookie}"
        try:
            with self._client.stream(
                method,
                self._root() + path,
                auth=None if cookie is not None else self._auth,
                headers=headers,
                data=data,
                files=files,
                json=document,
            ) as reply:
                if cookie is not None and reply.status_code in (302, 303, 401):
                    raise _SessionEnded
                if reply.status_code != 200:
                    raise UpstreamError(
                        f"the AeroFoil answered {reply.status_code} to {method} {path.split('?', maxsplit=1)[0]}"
                    )
                received = bytearray()
                for chunk in reply.iter_bytes():
                    received += chunk
                    if len(received) > limit:
                        raise UpstreamError(f"the answer is larger than {limit >> 20} MiB")
                return bytes(received)
        except httpx.HTTPError as error:
            raise UpstreamError(f"the AeroFoil cannot be reached ({type(error).__name__})") from error

    def _json(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        try:
            doc = json.loads(self.call(method, path, **kw))
        except ValueError as error:
            raise UpstreamError("the AeroFoil answered something that is not JSON") from error
        if not isinstance(doc, dict):
            raise UpstreamError("the AeroFoil answered something that is not an object")
        return doc

    # Cheats.

    def cheats(self) -> list[UpstreamCheat]:
        doc = self._json("GET", "/api/cheats")
        rows = doc.get("cheats")
        out: list[UpstreamCheat] = []
        for row in rows[:MAX_CHEATS] if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            title, build = _text(row.get("title_id")).upper(), _text(row.get("build_id")).upper()
            if title and build:
                out.append(UpstreamCheat(title, build, _int(row.get("size")), _text(row.get("note"))))
        return out

    def cheat(self, title_id: str, build_id: str, limit: int = 1 << 20) -> str:
        path = f"/api/cheats/{quote(title_id, safe='')}/{quote(build_id, safe='')}"
        return self.call("GET", path, limit=limit).decode("utf-8", "replace")

    def put_cheat(self, title_id: str, build_id: str, text: str, note: str) -> None:
        """Writes a cheat set (an administrator's call)."""
        self._json(
            "POST", "/api/cheats", data={"title_id": title_id, "build_id": build_id, "content": text, "note": note}
        )

    def delete_cheat(self, title_id: str, build_id: str) -> None:
        self._json("DELETE", f"/api/cheats/{quote(title_id, safe='')}/{quote(build_id, safe='')}")

    # Saves.

    def saves(self) -> list[UpstreamSave]:
        """Every backup of the user, of every type; the `latest` directories AeroFoil lists are left out."""
        doc = self._json("GET", "/api/saves/list?save_types=1")
        rows = doc.get("saves")
        out: list[UpstreamSave] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("latest_directory") or row.get("save_id") == "latest":
                continue
            kind = _text(row.get("save_type"))
            title, ident = _text(row.get("title_id")).upper(), _text(row.get("save_id"))
            if kind in SAVE_TYPES and title and ident:
                out.append(
                    UpstreamSave(
                        title, ident, kind, _int(row.get("size")), _int(row.get("created_ts")), _text(row.get("note"))
                    )
                )
        return out

    def save(self, title_id: str, save_id: str, save_type: str) -> bytes:
        path = f"/api/saves/download/{quote(title_id, safe='')}/{quote(save_id, safe='')}.zip?save_type={save_type}"
        return self.call("GET", path, limit=MAX_CALL)

    def put_save(self, title_id: str, save_type: str, note: str, archive: bytes) -> str:
        """Uploads a backup; the id AeroFoil gave it."""
        doc = self._json(
            "POST",
            f"/api/saves/upload/{quote(title_id, safe='')}",
            data={"save_type": save_type, "note": note},
            files={"file": ("save.zip", archive, "application/zip")},
            limit=MAX_LIST,
        )
        ident = doc.get("save_id")
        if not isinstance(ident, str) or not ident:
            raise UpstreamError("the AeroFoil did not name the backup it kept")
        return ident

    def delete_save(self, title_id: str, save_id: str, save_type: str) -> None:
        self._json(
            "DELETE", f"/api/saves/delete/{quote(title_id, safe='')}/{quote(save_id, safe='')}?save_type={save_type}"
        )

    # Requests.

    def requests(self) -> list[UpstreamRequest]:
        doc = self._json("GET", "/api/requests")
        rows = doc.get("requests")
        out: list[UpstreamRequest] = []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and isinstance(row.get("id"), int) and _text(row.get("title_id")):
                name = _text(row.get("title_name")) or None
                out.append(
                    UpstreamRequest(
                        row["id"],
                        _text(row["title_id"]).upper(),
                        name,
                        _text(row.get("status")),
                        _int(row.get("created_at")),
                    )
                )
        return out

    def ask(self, title_id: str, name: str | None) -> None:
        self._json("POST", "/api/requests", document={"title_id": title_id, "title_name": name or ""})
