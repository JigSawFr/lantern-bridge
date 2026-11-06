# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The configuration file: TOML, versioned by an integer ``schema``.

A reader skips what it does not know and keeps it when it writes the file
back. A file written by a newer schema is refused and never rewritten.
"""

from __future__ import annotations

import os
import tomllib
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import tomli_w
from lantern.uid import check_uid, new_uid

SCHEMA = 1
DEFAULT_NAME = "Lantern bridge"

HashMode = Literal["local", "remote"]
_HASH_MODES: tuple[HashMode, ...] = ("local", "remote")


class ConfigError(ValueError):
    """The file cannot be used; the message says why and what to change."""


class ConfigSchemaError(ConfigError):
    """The file comes from a newer version; it is left exactly as it is."""


@dataclass(frozen=True, slots=True)
class FolderSource:
    """A folder of files, read in place (a read-only volume is the best way)."""

    id: str
    path: Path


@dataclass(slots=True)
class Config:
    uid: str
    name: str = DEFAULT_NAME
    hash_mode: HashMode = "local"
    # False: every route that serves the library needs a paired device. True: anyone who can reach the server reads it.
    public: bool = False
    # True: the administration pages answer only to addresses of the local network (and the machine itself).
    admin_lan_only: bool = True
    sources: list[FolderSource] = field(default_factory=list)
    # Everything this version does not know, kept to be written back.
    _extra: dict[str, Any] = field(default_factory=dict, repr=False)
    _extra_sources: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    def to_toml(self) -> str:
        data = deepcopy(self._extra)
        data["schema"] = SCHEMA
        data["uid"] = self.uid
        data["name"] = self.name
        data["hash_mode"] = self.hash_mode
        data["public"] = self.public
        data["admin_lan_only"] = self.admin_lan_only
        data["sources"] = [
            {**self._extra_sources.get(s.id, {}), "id": s.id, "kind": "folder", "path": str(s.path)}
            for s in self.sources
        ]
        return tomli_w.dumps(data)


def _parse(data: dict[str, Any]) -> Config:
    schema = data.get("schema", SCHEMA)
    if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
        raise ConfigError("`schema` must be a positive integer")
    if schema > SCHEMA:
        raise ConfigSchemaError(
            f"the configuration has schema {schema}, this version reads up to {SCHEMA}: update lantern-bridge"
        )
    known = {"schema", "uid", "name", "hash_mode", "public", "admin_lan_only", "sources"}
    extra = {k: v for k, v in data.items() if k not in known}
    uid = data.get("uid")
    name = data.get("name", DEFAULT_NAME)
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("`name` must be a non-empty string")
    mode = data.get("hash_mode", "local")
    if mode not in _HASH_MODES:
        raise ConfigError(f"`hash_mode` must be one of {', '.join(_HASH_MODES)}")
    public = data.get("public", False)
    if not isinstance(public, bool):
        raise ConfigError("`public` must be true or false")
    lan_only = data.get("admin_lan_only", True)
    if not isinstance(lan_only, bool):
        raise ConfigError("`admin_lan_only` must be true or false")
    sources: list[FolderSource] = []
    extra_sources: dict[str, dict[str, Any]] = {}
    raw_sources = data.get("sources", [])
    if not isinstance(raw_sources, list):
        raise ConfigError("`sources` must be a list of tables")
    for raw in raw_sources:
        if not isinstance(raw, dict):
            raise ConfigError("each source must be a table")
        sid, kind, path = raw.get("id"), raw.get("kind"), raw.get("path")
        if not isinstance(sid, str) or not sid or not all(c.isalnum() or c in "-_" for c in sid):
            raise ConfigError("a source needs an `id` of letters, digits, `-` and `_`")
        if sid in extra_sources:
            raise ConfigError(f"source id {sid!r} is used twice")
        if kind != "folder":
            raise ConfigError(f"source {sid!r}: kind {kind!r} is not supported by this version")
        if not isinstance(path, str) or not path:
            raise ConfigError(f"source {sid!r}: `path` is required")
        sources.append(FolderSource(id=sid, path=Path(path)))
        extra_sources[sid] = {k: v for k, v in raw.items() if k not in {"id", "kind", "path"}}
    try:
        uid = check_uid(uid) if uid is not None else new_uid()
    except ValueError as error:
        raise ConfigError(f"`uid`: {error}") from error
    return Config(
        uid=uid,
        name=name,
        hash_mode=mode,
        public=public,
        admin_lan_only=lan_only,
        sources=sources,
        _extra=extra,
        _extra_sources=extra_sources,
    )


def load(path: Path) -> Config:
    """Read the file; a missing file gives the defaults with a new uid (not written)."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Config(uid=new_uid())
    try:
        data = tomllib.loads(raw.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigError(f"{path}: not valid TOML ({error})") from error
    return _parse(data)


def save(path: Path, config: Config) -> None:
    """Write the file atomically, 0600; refuse to replace a file of a newer schema."""
    if path.exists():
        load(path)  # raises ConfigSchemaError for a newer file, which stays untouched
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(config.to_toml())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def load_or_create(path: Path) -> Config:
    """Read the file, writing it first when absent so the uid never changes."""
    if not path.exists():
        save(path, config := Config(uid=new_uid()))
        return config
    config = load(path)
    if "uid" not in tomllib.loads(path.read_text(encoding="utf-8")):
        save(path, config)  # a file written by hand without a uid gets one, once
    return config
