# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The administrator of the pages: one local account, a session in a cookie.

The account lives in ``admin.json`` (0600, versioned by ``schema``). It is made
once, with a one-time code the server writes in its log, so that whoever
reaches the page first does not become the administrator.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = 1
SESSION_SECONDS = 8 * 3600
#: A session nobody used for this long ends, whatever is left of its eight hours.
IDLE_SECONDS = 30 * 60
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}
MIN_PASSWORD = 10


class AdminSchemaError(RuntimeError):
    """``admin.json`` comes from a newer version; it is left exactly as it is."""


def _hash(password: str, salt: bytes, n: int, r: int, p: int) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32).hex()


@dataclass(slots=True)
class Session:
    name: str
    csrf: str
    expires: float
    seen: float = 0.0
    #: "admin", or "observer": an account that looks at a few pages and changes nothing.
    role: str = "admin"
    #: Where it came from, for the list of open sessions (never the token, which only the cookie holds).
    address: str = ""
    agent: str = ""
    signed: float = 0.0  # when it was opened, by the clock on the wall


def short(token: str) -> str:
    """A session's name in the pages and in their forms: enough to tell it from the others, useless as a cookie."""
    return hashlib.sha256(token.encode()).hexdigest()[:12]


class AdminStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._setup_code: str | None = None

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        schema = data.get("schema", SCHEMA)
        if not isinstance(schema, int) or schema > SCHEMA:
            raise AdminSchemaError(f"{self.path} has schema {schema}, this version reads up to {SCHEMA}")
        return dict(data)

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({**data, "schema": SCHEMA}, handle, indent=2)
            tmp.replace(self.path)
        finally:
            tmp.unlink(missing_ok=True)

    @property
    def configured(self) -> bool:
        return bool(self._read().get("name"))

    def setup_code(self) -> str | None:
        """The one-time code (made on first ask), or None once an administrator exists."""
        with self._lock:
            if self.configured:
                self._setup_code = None
                return None
            if self._setup_code is None:
                raw = secrets.token_hex(4).upper()
                self._setup_code = f"{raw[:4]}-{raw[4:]}"
            return self._setup_code

    def create(self, code: str, name: str, password: str) -> str | None:
        """Make the administrator; the error to show, or None when it is done."""
        with self._lock:
            if self.configured:
                return "An administrator exists already."
            if self._setup_code is None or not secrets.compare_digest(code.strip().upper(), self._setup_code):
                return "That is not the setup code."
            if not name.strip() or len(name) > 64:
                return "Pick a name of 64 characters at most."
            if len(password) < MIN_PASSWORD:
                return f"Pick a password of at least {MIN_PASSWORD} characters."
            salt = secrets.token_bytes(16)
            data = self._read()
            data.update(name=name.strip(), salt=salt.hex(), hash=_hash(password, salt, **_SCRYPT), scrypt=_SCRYPT)
            self._write(data)
            self._setup_code = None
            return None

    @staticmethod
    def _matches(entry: dict[str, Any], name: str, password: str) -> bool:
        params = entry.get("scrypt", _SCRYPT)
        candidate = _hash(password, bytes.fromhex(entry["salt"]), params["n"], params["r"], params["p"])
        return secrets.compare_digest(name.strip(), entry["name"]) & secrets.compare_digest(candidate, entry["hash"])

    def identify(self, name: str, password: str) -> str | None:
        """The role of the account this name and password open ("admin" or "observer"), else None."""
        data = self._read()
        if not data.get("name"):
            return None
        admin = self._matches(data, name, password)
        observer = isinstance(data.get("observer"), dict) and self._matches(data["observer"], name, password)
        return "admin" if admin else "observer" if observer else None

    def check(self, name: str, password: str) -> bool:
        return self.identify(name, password) == "admin"

    @property
    def observer(self) -> str | None:
        """The name of the observer account, if there is one."""
        entry = self._read().get("observer")
        return entry.get("name") if isinstance(entry, dict) else None

    def set_observer(self, name: str, password: str) -> str | None:
        """Make or replace the observer; the error to show, or None when it is done."""
        name = name.strip()
        with self._lock:
            data = self._read()
            if not data.get("name"):
                return "There is no administrator yet."
            if not name or len(name) > 64:
                return "Pick a name of 64 characters at most."
            if name.casefold() == str(data["name"]).casefold():
                return "The observer needs a name of its own."
            if len(password) < MIN_PASSWORD:
                return f"Pick a password of at least {MIN_PASSWORD} characters."
            salt = secrets.token_bytes(16)
            data["observer"] = {
                "name": name,
                "salt": salt.hex(),
                "hash": _hash(password, salt, **_SCRYPT),
                "scrypt": _SCRYPT,
            }
            self._write(data)
            self._sessions = {t: s for t, s in self._sessions.items() if s.role != "observer"}
            return None

    def clear_observer(self) -> None:
        with self._lock:
            data = self._read()
            if data.pop("observer", None) is not None:
                self._write(data)
            self._sessions = {t: s for t, s in self._sessions.items() if s.role != "observer"}

    def login(self, name: str, role: str = "admin", address: str = "", agent: str = "") -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        session = Session(
            name,
            secrets.token_urlsafe(24),
            time.monotonic() + SESSION_SECONDS,
            seen=time.monotonic(),
            role=role,
            address=address[:64],
            agent=agent[:160],
            signed=time.time(),
        )
        with self._lock:
            self._sessions = {t: s for t, s in self._sessions.items() if s.expires > time.monotonic()}
            self._sessions[token] = session
        return token, session

    def session(self, token: str | None, touch: bool = True) -> Session | None:
        """The session of a cookie. Using it keeps it alive; `touch=False` (a page already open, asking again by
        itself) does not, so an open tab does not hold a session for ever."""
        if not token:
            return None
        now = time.monotonic()
        with self._lock:
            found = self._sessions.get(token)
            if found is None or found.expires <= now or now - found.seen > IDLE_SECONDS:
                self._sessions.pop(token, None)
                return None
            if touch:
                found.seen = now
            return found

    def logout(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(token, None)

    def listing(self, current: str | None = None) -> list[dict[str, Any]]:
        """The open sessions, newest first, for the administrator's page: who, from where, since when."""
        now, wall = time.monotonic(), time.time()
        with self._lock:
            self._sessions = {
                t: s for t, s in self._sessions.items() if s.expires > now and now - s.seen <= IDLE_SECONDS
            }
            rows = [
                {
                    "id": short(token),
                    "name": s.name,
                    "role": s.role,
                    "address": s.address,
                    "agent": s.agent,
                    "signed": s.signed,
                    "last": wall - (now - s.seen),
                    "current": token == current,
                }
                for token, s in self._sessions.items()
            ]
        return sorted(rows, key=lambda r: -r["signed"])

    def revoke(self, sid: str) -> bool:
        """Ends the session called `sid` (see `short`); False when there is none."""
        with self._lock:
            token = next((t for t in self._sessions if short(t) == sid), None)
            if token is None:
                return False
            del self._sessions[token]
            return True

    def revoke_others(self, current: str | None) -> int:
        """Ends every session but the one of this cookie; how many there were."""
        with self._lock:
            keep = {t: s for t, s in self._sessions.items() if t == current}
            ended = len(self._sessions) - len(keep)
            self._sessions = keep
            return ended
