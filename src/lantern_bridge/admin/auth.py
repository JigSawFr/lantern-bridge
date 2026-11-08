# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The operators of the pages: local accounts with a role, a session in a cookie.

The accounts live in ``admin.json`` (0600, versioned by ``schema``). The first one, the owner, is made once, with
a one-time code the server writes in its log, so that whoever reaches the page first does not become the owner.
The owner then adds the others.

Four roles, each able to everything the one before can: ``observer`` (looks at a few pages, changes nothing),
``manager`` (decides on consoles, accounts and invitations), ``admin`` (sources, settings, the library) and
``owner`` (the operators, backups and imports as well). What each may open is in ``permissions.py``.
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

SCHEMA = 2
SESSION_SECONDS = 8 * 3600
#: A session nobody used for this long ends, whatever is left of its eight hours.
IDLE_SECONDS = 30 * 60
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}
#: The fewest characters an operator's password may have (a length, not a secret).
MIN_LENGTH = 10
#: From the least to the most: a role may do what the ones before it may.
ROLES = ("observer", "manager", "admin", "owner")


def rank(role: str | None) -> int:
    """How far a role goes (0 for none, or a role this version does not know)."""
    return ROLES.index(role) + 1 if role in ROLES else 0


class AdminSchemaError(RuntimeError):
    """``admin.json`` comes from a newer version; it is left exactly as it is."""


def _hash(password: str, salt: bytes, n: int, r: int, p: int) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32).hex()


def _entry(name: str, role: str, password: str) -> dict[str, Any]:
    salt = secrets.token_bytes(16)
    return {
        "name": name,
        "role": role,
        "salt": salt.hex(),
        "hash": _hash(password, salt, **_SCRYPT),
        "scrypt": _SCRYPT,
        "created": time.time(),
    }


@dataclass(frozen=True, slots=True)
class Operator:
    name: str
    role: str
    created: float
    last: float | None  # the last sign-in, by the clock on the wall


@dataclass(slots=True)
class Session:
    name: str
    csrf: str
    expires: float
    seen: float = 0.0
    #: One of ROLES; kept in step with the account when the owner changes it.
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
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        self._setup_code: str | None = None
        self._take_up()

    def _take_up(self) -> None:
        """Takes up a file of schema 1 (one administrator and an optional observer, at the top) as schema 2 (a
        list of operators): the administrator becomes the owner. Written once, the old keys gone; this goes
        when no file of schema 1 is left to open (the bridge has had no release yet)."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return
        if not isinstance(data, dict) or data.get("schema", 1) != 1:
            return
        operators: list[dict[str, Any]] = []
        if data.get("name"):
            keep = {k: data[k] for k in ("name", "salt", "hash", "scrypt") if k in data}
            operators.append({**keep, "role": "owner", "created": time.time()})
        observer = data.get("observer")
        if isinstance(observer, dict) and observer.get("name"):
            operators.append({**observer, "role": "observer", "created": time.time()})
        for key in ("name", "salt", "hash", "scrypt", "observer"):
            data.pop(key, None)
        data["operators"] = operators
        self._write(data)

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

    def _entries(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        found = data.get("operators")
        return [e for e in found if isinstance(e, dict) and e.get("name")] if isinstance(found, list) else []

    @property
    def configured(self) -> bool:
        return any(e.get("role") == "owner" for e in self._entries(self._read()))

    def setup_code(self) -> str | None:
        """The one-time code (made on first ask), or None once an owner exists."""
        with self._lock:
            if self.configured:
                self._setup_code = None
                return None
            if self._setup_code is None:
                raw = secrets.token_hex(4).upper()
                self._setup_code = f"{raw[:4]}-{raw[4:]}"
            return self._setup_code

    @staticmethod
    def _problem(name: str, password: str) -> str | None:
        if not name or len(name) > 64:
            return "Pick a name of 64 characters at most."
        if len(password) < MIN_LENGTH:
            return f"Pick a password of at least {MIN_LENGTH} characters."
        return None

    def create(self, code: str, name: str, password: str) -> str | None:
        """Make the owner; the error to show, or None when it is done."""
        name = name.strip()
        with self._lock:
            if self.configured:
                return "An administrator exists already."
            if self._setup_code is None or not secrets.compare_digest(code.strip().upper(), self._setup_code):
                return "That is not the setup code."
            if problem := self._problem(name, password):
                return problem
            data = self._read()
            data["operators"] = [*self._entries(data), _entry(name, "owner", password)]
            self._write(data)
            self._setup_code = None
            return None

    @staticmethod
    def _matches(entry: dict[str, Any], password: str) -> bool:
        params = entry.get("scrypt", _SCRYPT)
        candidate = _hash(password, bytes.fromhex(entry["salt"]), params["n"], params["r"], params["p"])
        return secrets.compare_digest(candidate, entry["hash"])

    def identify(self, name: str, password: str) -> str | None:
        """The role of the operator this name and password open, else None. An unknown name costs as long as a
        wrong password does."""
        name = name.strip()
        found = next((e for e in self._entries(self._read()) if e["name"] == name), None)
        if found is None:
            _hash(password, b"\0" * 16, **_SCRYPT)
            return None
        role = str(found.get("role", ""))
        return role if self._matches(found, password) and role in ROLES else None

    def operators(self) -> list[Operator]:
        """The operators, the owners first and then by name."""
        rows = [
            Operator(str(e["name"]), str(e.get("role", "")), float(e.get("created") or 0), e.get("last"))
            for e in self._entries(self._read())
        ]
        return sorted(rows, key=lambda o: (-rank(o.role), o.name.casefold()))

    def _owners(self, entries: list[dict[str, Any]]) -> int:
        return sum(1 for e in entries if e.get("role") == "owner")

    def add(self, name: str, role: str, password: str) -> str | None:
        """Adds an operator; the error to show, or None when it is done."""
        name = name.strip()
        with self._lock:
            data = self._read()
            entries = self._entries(data)
            if not entries:
                return "There is no administrator yet."
            if role not in ROLES:
                return "Choose a role from the list."
            if problem := self._problem(name, password):
                return problem
            if any(str(e["name"]).casefold() == name.casefold() for e in entries):
                return "An operator has that name already."
            data["operators"] = [*entries, _entry(name, role, password)]
            self._write(data)
            return None

    def set_role(self, name: str, role: str) -> str | None:
        with self._lock:
            data = self._read()
            entries = self._entries(data)
            found = next((e for e in entries if e["name"] == name), None)
            if found is None:
                return "No such operator."
            if role not in ROLES:
                return "Choose a role from the list."
            if found.get("role") == "owner" and role != "owner" and self._owners(entries) < 2:
                return "The last owner keeps that role."
            found["role"] = role
            data["operators"] = entries
            self._write(data)
            for session in self._sessions.values():
                if session.name == name:
                    session.role = role
            return None

    def set_password(self, name: str, password: str, keep: str | None = None) -> str | None:
        """A new password for an operator; the sessions it had end, but for the one of the cookie `keep`."""
        with self._lock:
            data = self._read()
            entries = self._entries(data)
            found = next((e for e in entries if e["name"] == name), None)
            if found is None:
                return "No such operator."
            if len(password) < MIN_LENGTH:
                return f"Pick a password of at least {MIN_LENGTH} characters."
            found.update(
                {k: v for k, v in _entry(name, str(found.get("role", "")), password).items() if k != "created"}
            )
            data["operators"] = entries
            self._write(data)
            self._drop(name, keep)
            return None

    def change_own_password(self, name: str, current: str, password: str, keep: str | None) -> str | None:
        """An operator's own change: it needs the password it has; its other sessions end."""
        with self._lock:
            found = next((e for e in self._entries(self._read()) if e["name"] == name), None)
            if found is None or not self._matches(found, current):
                return "The current password is not right."
            return self.set_password(name, password, keep)

    def remove(self, name: str) -> str | None:
        with self._lock:
            data = self._read()
            entries = self._entries(data)
            found = next((e for e in entries if e["name"] == name), None)
            if found is None:
                return "No such operator."
            if found.get("role") == "owner" and self._owners(entries) < 2:
                return "The last owner keeps that role."
            data["operators"] = [e for e in entries if e is not found]
            self._write(data)
            self._drop(name, None)
            return None

    def _drop(self, name: str, keep: str | None) -> None:
        self._sessions = {t: s for t, s in self._sessions.items() if s.name != name or t == keep}

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
            data = self._read()
            entries = self._entries(data)
            if found := next((e for e in entries if e["name"] == name), None):
                found["last"] = session.signed
                data["operators"] = entries
                self._write(data)
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
