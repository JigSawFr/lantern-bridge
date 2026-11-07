# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""An AeroFoil as a source: a Tinfoil shop that also keeps saves, cheats and requests.

AeroFoil answers its shop index like any Tinfoil shop (``Shop``); the CyberFoil functions are plain HTTP
endpoints under ``/api``, called here with the login of a user: the service account of the source, or the
one a Lantern account is tied to.
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


class AeroFoil(Shop):
    """One AeroFoil, read with one user's login."""

    def as_user(self, user: str | None, password: str | None) -> AeroFoil:
        """The same AeroFoil, called as another user (the connection is shared)."""
        return AeroFoil(self.url, user=user, password=password, client=self._client, hauth=self.hauth)

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
        try:
            with self._client.stream(
                method,
                self._root() + path,
                auth=self._auth,
                headers=self.headers(Accept="application/json, */*"),
                data=data,
                files=files,
                json=document,
            ) as reply:
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
