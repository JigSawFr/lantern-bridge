# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""What an AeroFoil keeps for the accounts: saves, cheats and requests.

Lantern's server keeps its own copy (a folder of backups, a library of cheat sets, a table of requests) and
mirrors it to the AeroFoil, so that CyberFoil and the AeroFoil's own pages see the same things:

- saves go both ways: a backup made through Lantern is uploaded, one that only the AeroFoil has is imported;
- cheats are read from the AeroFoil, and a set written through Lantern is written there too (the login must
  be an administrator's);
- a request is made on the AeroFoil too, and its answer (closed, denied) comes back as ``done`` or ``declined``.

Which AeroFoil user an account acts as comes from the source's ``logins``; an account with none acts as the
service account, if the source has one. Nothing is mirrored for an account that has neither.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lantern._generated.models import CheatSet, Save
from lantern.digest import sha256_hex
from lantern.server import MemoryLibrary
from lantern.server.files import FileRef
from lantern.server.http import ProblemError
from lantern.server.saves import ZIP_STARTS, FileSaveStore, UploadSession, new_save_id
from lantern.server.title_requests import StoredRequest

from lantern_bridge.config import AerofoilSource
from lantern_bridge.sources.aerofoil import AeroFoil, UpstreamSave
from lantern_bridge.sources.tinfoil import UpstreamError

log = logging.getLogger("lantern_bridge.upstream")

PLATFORM = "switch"
#: Seconds between two reads of an account's backups on the AeroFoil.
SAVES_EVERY = 60.0
#: The largest backup brought over from the AeroFoil.
MAX_IMPORT = 256 << 20
#: The kinds of backup the AeroFoil keeps.
MIRRORED = ("account", "device")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS pair (user TEXT, save_id TEXT, title TEXT, kind TEXT, up_id TEXT,
                                 PRIMARY KEY (user, save_id));
CREATE TABLE IF NOT EXISTS ask (id TEXT PRIMARY KEY, user TEXT, platform TEXT, title_id TEXT, name TEXT,
                                status TEXT, created TEXT, updated TEXT, reason TEXT, asked INTEGER);
"""


def _iso(seconds: int) -> str:
    return datetime.fromtimestamp(seconds or time.time(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ledger:
    """Which local backup is which backup of the AeroFoil, and the requests (SQLite, one writer)."""

    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.executescript(_SCHEMA)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def run(self, sql: str, *args: object) -> list[tuple[object, ...]]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()


class Logins:
    """The AeroFoil an account acts on, and as whom."""

    def __init__(self, source: AerofoilSource, shop: AeroFoil, environ: dict[str, str] | None = None) -> None:
        self.source, self.shop = source, shop
        self._env = environ if environ is not None else os.environ
        self._by_account = {x.account: x for x in source.logins}
        self._made: dict[str, AeroFoil] = {}

    def of(self, owner: str) -> AeroFoil | None:
        login = self._by_account.get(owner)
        if login is not None:
            password = self._env.get(login.password_env)
            if password is None:
                log.warning("the variable %s is not set: %s acts as the service account", login.password_env, owner)
            else:
                made = self._made.get(owner)
                if made is None:
                    made = self._made[owner] = self.shop.as_user(login.user, password)
                return made
        return self.shop if self.source.user is not None else None


class AeroSaves:
    """Save backups: a folder, mirrored to the AeroFoil. It is a SaveStore."""

    def __init__(self, folder: Path, logins: Logins, ledger: Ledger, *, every: float = SAVES_EVERY) -> None:
        self.inner = FileSaveStore(folder)
        self.logins, self.ledger, self.every = logins, ledger, every
        self._seen: dict[str, float] = {}

    # Mirroring.

    def _push(self, user: str, save: Save, data: bytes) -> None:
        shop = self.logins.of(user)
        if shop is None or save.kind not in MIRRORED:
            return
        try:
            up = shop.put_save(save.title_id.upper(), save.kind or "account", save.note or "", data)
        except UpstreamError as error:
            log.warning("the backup %s is kept here only, for now: %s", save.save_id, error)
            return
        self.ledger.run(
            "INSERT OR REPLACE INTO pair VALUES (?,?,?,?,?)", user, save.save_id, save.title_id.upper(), save.kind, up
        )

    def _sync(self, user: str) -> None:
        shop = self.logins.of(user)
        if shop is None or time.monotonic() - self._seen.get(user, -1e9) < self.every:
            return
        self._seen[user] = time.monotonic()
        try:
            there = shop.saves()
        except UpstreamError as error:
            log.warning("the backups of %s cannot be read from the AeroFoil: %s", user, error)
            return
        paired = {
            str(r[1]): (str(r[2]), str(r[3]), str(r[4]))
            for r in self.ledger.run("SELECT user, save_id, title, kind, up_id FROM pair WHERE user=?", user)
        }
        known = {up for _, _, up in paired.values()}
        for up in there:
            if up.save_id not in known:
                self._import(user, shop, up)
        names = {(u.title_id, u.save_type, u.save_id) for u in there}
        for save_id, (title, kind, up_id) in paired.items():
            if (title, kind, up_id) not in names:  # deleted on the AeroFoil
                self.inner.drop(user, save_id)
                self.ledger.run("DELETE FROM pair WHERE user=? AND save_id=?", user, save_id)
        now = {str(r[0]) for r in self.ledger.run("SELECT save_id FROM pair WHERE user=?", user)}
        for save in self.inner.saves(user):
            if save.save_id not in now and save.kind in MIRRORED:  # kept while the AeroFoil was away
                ref = self.inner.archive(user, save.save_id)
                if ref is not None:
                    self._push(user, save, _bytes(ref))

    def _import(self, user: str, shop: AeroFoil, up: UpstreamSave) -> None:
        if up.size > MAX_IMPORT:
            return
        try:
            data = shop.save(up.title_id, up.save_id, up.save_type)
        except UpstreamError as error:
            log.warning("the backup %s of the AeroFoil is not brought over: %s", up.save_id, error)
            return
        if not data.startswith(ZIP_STARTS):
            return
        save = Save(
            platform=PLATFORM,
            title_id=up.title_id,
            save_id=new_save_id(),
            created=_iso(up.created),
            size=len(data),
            sha256=sha256_hex(data),
            kind=up.save_type if up.save_type in MIRRORED else "other",
            note=up.note or None,
        )
        self.inner.keep(user, save, data)
        self.ledger.run(
            "INSERT OR REPLACE INTO pair VALUES (?,?,?,?,?)", user, save.save_id, up.title_id, up.save_type, up.save_id
        )

    # SaveStore.

    def saves(self, user: str) -> list[Save]:
        self._sync(user)
        return self.inner.saves(user)

    def archive(self, user: str, save_id: str) -> FileRef | None:
        return self.inner.archive(user, save_id)

    def keep(self, user: str, save: Save, data: bytes) -> None:
        self.inner.keep(user, save, data)
        self._push(user, save, data)

    def drop(self, user: str, save_id: str) -> bool:
        rows = self.ledger.run("SELECT title, kind, up_id FROM pair WHERE user=? AND save_id=?", user, save_id)
        shop = self.logins.of(user)
        if rows and shop is not None:
            try:
                shop.delete_save(str(rows[0][0]), str(rows[0][2]), str(rows[0][1]))
            except UpstreamError as error:
                log.warning("the backup %s is removed here, not on the AeroFoil: %s", save_id, error)
        self.ledger.run("DELETE FROM pair WHERE user=? AND save_id=?", user, save_id)
        return self.inner.drop(user, save_id)

    def open_upload(self, session: UploadSession) -> None:
        self.inner.open_upload(session)

    def upload(self, upload_id: str) -> UploadSession | None:
        return self.inner.upload(upload_id)

    def uploads(self) -> list[UploadSession]:
        return self.inner.uploads()

    def append(self, upload_id: str, data: bytes) -> int:
        return self.inner.append(upload_id, data)

    def upload_digest(self, upload_id: str) -> tuple[str, bytes]:
        return self.inner.upload_digest(upload_id)

    def keep_upload(self, upload_id: str, save: Save) -> None:
        session = self.inner.upload(upload_id)
        self.inner.keep_upload(upload_id, save)
        ref = self.inner.archive(session.user, save.save_id) if session is not None else None
        if session is not None and ref is not None:
            self._push(session.user, save, _bytes(ref))

    def abandon(self, upload_id: str) -> None:
        self.inner.abandon(upload_id)

    def forget(self, user: str) -> None:
        self.ledger.run("DELETE FROM pair WHERE user=?", user)  # the AeroFoil keeps its copies
        self.inner.forget(user)


def _bytes(ref: FileRef) -> bytes:
    return ref.data if ref.data is not None else Path(ref.path).read_bytes()


class AeroRequests:
    """Requests for games, kept in the ledger and made on the AeroFoil too. It is a RequestStore."""

    def __init__(self, logins: Logins, ledger: Ledger) -> None:
        self.logins, self.ledger = logins, ledger

    @staticmethod
    def _row(r: tuple[object, ...]) -> StoredRequest:
        s = [x if isinstance(x, str) else None for x in r]
        return StoredRequest(
            id=str(r[0]),
            user=str(r[1]),
            platform=str(r[2]),
            title_id=str(r[3]),
            name=s[4],
            status=str(r[5]),
            created=str(r[6]),
            updated=s[7],
            reason=s[8],
        )

    _COLUMNS = "id, user, platform, title_id, name, status, created, updated, reason"

    def requests(self, user: str | None = None) -> list[StoredRequest]:
        sql = f"SELECT {self._COLUMNS} FROM ask"  # noqa: S608 (a constant list of columns)
        args: tuple[str, ...] = ()
        if user is not None:
            sql, args = sql + " WHERE user=?", (user,)
        return [self._row(r) for r in self.ledger.run(sql + " ORDER BY created", *args)]

    def add(self, request: StoredRequest) -> None:
        asked = 0
        shop = self.logins.of(request.user)
        if shop is not None:
            try:
                shop.ask(request.title_id.upper(), request.name)
                asked = 1
            except UpstreamError as error:
                log.warning("the request for %s is kept here only: %s", request.title_id, error)
        self.ledger.run(
            f"INSERT OR REPLACE INTO ask ({self._COLUMNS}, asked) VALUES (?,?,?,?,?,?,?,?,?,?)",  # noqa: S608
            request.id,
            request.user,
            request.platform,
            request.title_id,
            request.name,
            request.status,
            request.created,
            request.updated,
            request.reason,
            asked,
        )

    def set_status(
        self, request_id: str, status: str, *, reason: str | None = None, updated: str | None = None
    ) -> StoredRequest | None:
        self.ledger.run("UPDATE ask SET status=?, reason=?, updated=? WHERE id=?", status, reason, updated, request_id)
        rows = self.ledger.run(f"SELECT {self._COLUMNS} FROM ask WHERE id=?", request_id)  # noqa: S608
        return self._row(rows[0]) if rows else None

    def remove(self, user: str, request_id: str) -> StoredRequest | None:
        rows = self.ledger.run(f"SELECT {self._COLUMNS} FROM ask WHERE id=? AND user=?", request_id, user)  # noqa: S608
        if not rows:
            return None
        self.ledger.run("DELETE FROM ask WHERE id=?", request_id)
        return self._row(rows[0])

    def forget(self, user: str) -> None:
        self.ledger.run("DELETE FROM ask WHERE user=?", user)

    def pull(self, answer: Callable[[str, str, str | None], object]) -> None:
        """Hands `answer(id, "done"|"declined", reason)` the open requests the AeroFoil has decided
        (``closed``: the title came; ``denied``)."""
        for user in sorted({r.user for r in self.requests() if r.status == "open"}):
            shop = self.logins.of(user)
            if shop is None:
                continue
            try:
                decided = {u.title_id: u.status for u in shop.requests()}
            except UpstreamError as error:
                log.warning("the requests of %s cannot be read from the AeroFoil: %s", user, error)
                continue
            for r in self.requests(user):
                verdict = {"closed": "done", "denied": "declined"}.get(decided.get(r.title_id.upper(), ""))
                if r.status == "open" and verdict is not None:
                    answer(r.id, verdict, "Declined on the AeroFoil." if verdict == "declined" else None)


class BridgeLibrary(MemoryLibrary):
    """A MemoryLibrary whose cheat sets, written by a client, are written to the AeroFoil first."""

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.shop: AeroFoil | None = None
        self.written: Callable[[CheatSet], None] | None = None

    def write_cheat(self, **kw: Any) -> CheatSet:
        build = kw.get("build_id")
        if self.shop is not None and build:
            try:
                self.shop.put_cheat(kw["title_id"].upper(), build.upper(), kw["text"], kw.get("note") or "")
            except UpstreamError as error:
                raise ProblemError(502, "bad_gateway", f"The AeroFoil refused the cheat set: {error}") from error
        made = super().write_cheat(**kw)
        if self.written is not None and self.shop is not None and build:
            self.written(made)
        return made

    def delete_cheat(self, cheat_id: str) -> bool:
        found = next((c for c in self.cheats() if c.id == cheat_id), None)
        if found is not None and self.shop is not None and found.build_id:
            try:
                self.shop.delete_cheat(found.title_id.upper(), found.build_id.upper())
            except UpstreamError as error:
                raise ProblemError(
                    502, "bad_gateway", f"The AeroFoil refused to remove the cheat set: {error}"
                ) from error
        return super().delete_cheat(cheat_id)

    def forget_cheat(self, cheat_id: str) -> bool:
        """Removes a set from the library only (it is gone from the AeroFoil already)."""
        return MemoryLibrary.delete_cheat(self, cheat_id)


class Cheats:
    """The cheat sets of an AeroFoil, kept in the library under the source's name."""

    def __init__(self, library: BridgeLibrary, source: str) -> None:
        self.library, self.source = library, source
        self._kept: dict[tuple[str, str], tuple[str, int, str]] = {}  # (title, build) -> (id, size, note)
        library.written = self.adopt

    def adopt(self, cheat: CheatSet) -> None:
        """A set a client wrote, and the AeroFoil took: it is that (title, build)'s set from now on."""
        key = (cheat.title_id.upper(), (cheat.build_id or "").upper())
        held = self._kept.get(key)
        if held is not None and held[0] != cheat.id:
            self.library.forget_cheat(held[0])
        self._kept[key] = (cheat.id, cheat.size, cheat.note or "")

    def refresh(self, shop: AeroFoil) -> None:
        listed = {(c.title_id, c.build_id): c for c in shop.cheats()}
        for key, (cheat_id, _, _) in list(self._kept.items()):
            if key not in listed:
                self.library.forget_cheat(cheat_id)
                del self._kept[key]
        for key, cheat in listed.items():
            held = self._kept.get(key)
            if held is not None and held[1:] == (cheat.size, cheat.note):
                continue
            try:
                text = shop.cheat(*key)
                made = self.library.add_cheat(
                    platform=PLATFORM,
                    title_id=key[0],
                    build_id=key[1],
                    text=text,
                    source=self.source,
                    note=cheat.note or None,
                )
            except (UpstreamError, ValueError) as error:
                log.warning("the cheats of %s/%s are not taken: %s", *key, error)
                continue
            if held is not None:
                self.library.forget_cheat(held[0])
            self._kept[key] = (made.id, cheat.size, cheat.note)
