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
from typing import Any
from urllib.parse import urlsplit

import tomli_w
from lantern.uid import check_uid, new_uid

SCHEMA = 1
DEFAULT_NAME = "Lantern bridge"


class ConfigError(ValueError):
    """The file cannot be used; the message says why and what to change."""


class ConfigSchemaError(ConfigError):
    """The file comes from a newer version; it is left exactly as it is."""


@dataclass(frozen=True, slots=True)
class FolderSource:
    """A folder of files, read in place (a read-only volume is the best way)."""

    id: str
    path: Path

    @property
    def location(self) -> str:
        return str(self.path)


@dataclass(frozen=True, slots=True)
class ShopSource:
    """A shop read over HTTP.

    The password is never in the file: ``password_env`` names the environment variable that holds it.
    """

    id: str
    url: str
    user: str | None = None
    password_env: str | None = None

    @property
    def location(self) -> str:
        """The address as the pages show it: never with a login in it."""
        parts = urlsplit(self.url)
        if parts.username is None and parts.password is None:
            return self.url
        return parts._replace(netloc=parts.netloc.rsplit("@", 1)[-1]).geturl()


@dataclass(frozen=True, slots=True)
class TinfoilSource(ShopSource):
    """A Tinfoil shop: any server that answers a JSON index."""


@dataclass(frozen=True, slots=True)
class OwnfoilSource(ShopSource):
    """An Ownfoil: its GraphQL API lists the files and gives the sheets and the images."""


@dataclass(frozen=True, slots=True)
class Login:
    """A Lantern account's login on an AeroFoil (its saves and requests are that user's); the password
    is in the environment variable ``password_env``."""

    account: str
    user: str
    password_env: str


@dataclass(frozen=True, slots=True)
class AerofoilSource(ShopSource):
    """An AeroFoil: a Tinfoil shop that also keeps saves, cheats and requests (the CyberFoil functions).

    ``user`` and ``password_env`` are the service account (it reads the shop and the cheats, and keeps the
    saves of an account with no login); ``logins`` give each Lantern account its own AeroFoil user.
    """

    logins: tuple[Login, ...] = ()


Source = FolderSource | TinfoilSource | OwnfoilSource | AerofoilSource


@dataclass(slots=True)
class Mqtt:
    """The broker the bridge publishes its state on (and takes a few orders from). Absent: nothing is published."""

    host: str
    port: int = 1883
    user: str | None = None
    # The name of the environment variable that holds the password, as for a source.
    password_env: str | None = None
    prefix: str = "lantern-bridge"
    tls: bool = False
    # Everything this version does not know, kept to be written back.
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    def table(self) -> dict[str, Any]:
        table: dict[str, Any] = {
            **self.extra,
            "host": self.host,
            "port": self.port,
            "prefix": self.prefix,
            "tls": self.tls,
        }
        if self.user is not None:
            table["user"] = self.user
        if self.password_env is not None:
            table["password_env"] = self.password_env
        return table


@dataclass(slots=True)
class Config:
    uid: str
    name: str = DEFAULT_NAME
    # Megabytes per second read from a shop that is hashed over the network (0: no limit).
    remote_rate: float = 10.0
    # False: every route that serves the library needs a paired device. True: anyone who can reach the server reads it.
    public: bool = False
    # True: the administration pages answer only to addresses of the local network (and the machine itself).
    admin_lan_only: bool = True
    # True: the administration pages add a developer page (requests, raw JSON, timings). Off by default.
    developer: bool = False
    # True: titles get their genres, developer and publisher from Atlas (a public file fetched once a day).
    atlas: bool = True
    # True: /metrics answers, in the Prometheus text format, to addresses of the local network. Off by default.
    metrics: bool = False
    mqtt: Mqtt | None = None
    # Megabytes free on the volume of the state below which the health says to look (it fails below 20).
    low_disk_mb: int = 500
    # True: the bridge reads GitHub's latest release once a day and says on the overview when newer. On by default.
    update_check: bool = True
    # An address that gets a POST when scans found changes (a Home Assistant webhook, ntfy, any tool). None: no call.
    webhook: str | None = None
    sources: list[Source] = field(default_factory=list)
    # Everything this version does not know, kept to be written back.
    _extra: dict[str, Any] = field(default_factory=dict, repr=False)
    _extra_sources: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    def to_toml(self) -> str:
        data = deepcopy(self._extra)
        data["schema"] = SCHEMA
        data["uid"] = self.uid
        data["name"] = self.name
        data["remote_rate"] = self.remote_rate
        data["public"] = self.public
        data["admin_lan_only"] = self.admin_lan_only
        data["developer"] = self.developer
        data["atlas"] = self.atlas
        data["metrics"] = self.metrics
        data["update_check"] = self.update_check
        data["low_disk_mb"] = self.low_disk_mb
        if self.mqtt is not None:
            data["mqtt"] = self.mqtt.table()
        if self.webhook is not None:
            data["webhook"] = self.webhook
        data["sources"] = [{**self._extra_sources.get(s.id, {}), **_source_table(s)} for s in self.sources]
        return tomli_w.dumps(data)


def _source_table(source: Source) -> dict[str, Any]:
    if isinstance(source, FolderSource):
        return {"id": source.id, "kind": "folder", "path": str(source.path)}
    kind = {OwnfoilSource: "ownfoil", AerofoilSource: "aerofoil"}.get(type(source), "tinfoil")
    table: dict[str, Any] = {"id": source.id, "kind": kind, "url": source.url}
    if source.user is not None:
        table["user"] = source.user
    if source.password_env is not None:
        table["password_env"] = source.password_env
    if isinstance(source, AerofoilSource) and source.logins:
        table["logins"] = [
            {"account": x.account, "user": x.user, "password_env": x.password_env} for x in source.logins
        ]
    return table


def parse_webhook(raw: object) -> str | None:
    """The address that gets a POST, or None (an empty text is none)."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ConfigError("`webhook` must be an address")
    parsed = urlsplit(raw.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError("`webhook` must be an http or https address")
    if parsed.username or parsed.password:
        raise ConfigError("no credentials in the `webhook` address: put a secret in its path if the service wants one")
    return raw.strip()


def parse_mqtt(raw: object) -> Mqtt | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("`mqtt` must be a table")
    host, port, prefix, tls = (
        raw.get("host"),
        raw.get("port", 1883),
        raw.get("prefix", "lantern-bridge"),
        raw.get("tls", False),
    )
    if not isinstance(host, str) or not host.strip():
        raise ConfigError("`mqtt.host` is required")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise ConfigError("`mqtt.port` must be a port number")
    if (
        not isinstance(prefix, str)
        or not prefix.strip("/")
        or any(c in prefix for c in "+#")
        or prefix != prefix.strip("/")
    ):
        raise ConfigError("`mqtt.prefix` must be a topic without wildcards or slashes at its ends")
    if not isinstance(tls, bool):
        raise ConfigError("`mqtt.tls` must be true or false")
    user, env = raw.get("user"), raw.get("password_env")
    for key, value in (("user", user), ("password_env", env)):
        if value is not None and (not isinstance(value, str) or not value):
            raise ConfigError(f"`mqtt.{key}` must be a non-empty string")
    if (user is None) != (env is None):
        raise ConfigError("`mqtt.user` and `mqtt.password_env` go together")
    own = {"host", "port", "user", "password_env", "prefix", "tls"}
    return Mqtt(host.strip(), port, user, env, prefix, tls, {k: v for k, v in raw.items() if k not in own})


def _logins(sid: str, raw: dict[str, Any]) -> tuple[Login, ...]:
    rows = raw.get("logins", [])
    if not isinstance(rows, list):
        raise ConfigError(f"source {sid!r}: `logins` must be a list of tables")
    out: list[Login] = []
    for row in rows:
        fields = [row.get(k) if isinstance(row, dict) else None for k in ("account", "user", "password_env")]
        if not all(isinstance(f, str) and f for f in fields):
            raise ConfigError(f"source {sid!r}: a login needs `account`, `user` and `password_env`")
        if any(x.account == fields[0] for x in out):
            raise ConfigError(f"source {sid!r}: the account {fields[0]!r} has two logins")
        out.append(Login(*fields))  # type: ignore[arg-type]
    return tuple(out)


def _parse_url(sid: str, url: object) -> str:
    if not isinstance(url, str) or not url:
        raise ConfigError(f"source {sid!r}: `url` is required")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError(f"source {sid!r}: `url` must be an http or https address")
    if parsed.username or parsed.password:
        raise ConfigError(f"source {sid!r}: no credentials in the `url`: use `user` and `password_env`")
    return url


def _parse(data: dict[str, Any]) -> Config:
    schema = data.get("schema", SCHEMA)
    if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
        raise ConfigError("`schema` must be a positive integer")
    if schema > SCHEMA:
        raise ConfigSchemaError(
            f"the configuration has schema {schema}, this version reads up to {SCHEMA}: update lantern-bridge"
        )
    known = {
        "schema",
        "uid",
        "name",
        "remote_rate",
        "public",
        "admin_lan_only",
        "developer",
        "atlas",
        "metrics",
        "update_check",
        "low_disk_mb",
        "mqtt",
        "webhook",
        "sources",
    }
    extra = {k: v for k, v in data.items() if k not in known}
    uid = data.get("uid")
    name = data.get("name", DEFAULT_NAME)
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("`name` must be a non-empty string")
    rate = data.get("remote_rate", 10.0)
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate < 0:
        raise ConfigError("`remote_rate` must be a number of megabytes per second, 0 or more")
    public = data.get("public", False)
    if not isinstance(public, bool):
        raise ConfigError("`public` must be true or false")
    lan_only = data.get("admin_lan_only", True)
    if not isinstance(lan_only, bool):
        raise ConfigError("`admin_lan_only` must be true or false")
    developer = data.get("developer", False)
    if not isinstance(developer, bool):
        raise ConfigError("`developer` must be true or false")
    atlas = data.get("atlas", True)
    if not isinstance(atlas, bool):
        raise ConfigError("`atlas` must be true or false")
    metrics = data.get("metrics", False)
    if not isinstance(metrics, bool):
        raise ConfigError("`metrics` must be true or false")
    low_disk = data.get("low_disk_mb", 500)
    if isinstance(low_disk, bool) or not isinstance(low_disk, int) or not 20 <= low_disk <= 10_000_000:
        raise ConfigError("`low_disk_mb` must be a number of megabytes, 20 or more")
    update_check = data.get("update_check", True)
    if not isinstance(update_check, bool):
        raise ConfigError("`update_check` must be true or false")
    mqtt = parse_mqtt(data.get("mqtt"))
    webhook = parse_webhook(data.get("webhook"))
    sources: list[Source] = []
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
        if kind == "folder":
            if not isinstance(path, str) or not path:
                raise ConfigError(f"source {sid!r}: `path` is required")
            sources.append(FolderSource(id=sid, path=Path(path)))
            own = {"id", "kind", "path"}
        elif kind in ("tinfoil", "ownfoil", "aerofoil"):
            user, env = raw.get("user"), raw.get("password_env")
            for key, value in (("user", user), ("password_env", env)):
                if value is not None and (not isinstance(value, str) or not value):
                    raise ConfigError(f"source {sid!r}: `{key}` must be a non-empty string")
            if (user is None) != (env is None):
                raise ConfigError(f"source {sid!r}: `user` and `password_env` go together")
            url = _parse_url(sid, raw.get("url"))
            if kind == "aerofoil":
                sources.append(AerofoilSource(id=sid, url=url, user=user, password_env=env, logins=_logins(sid, raw)))
                own = {"id", "kind", "url", "user", "password_env", "logins"}
            else:
                shop = OwnfoilSource if kind == "ownfoil" else TinfoilSource
                sources.append(shop(id=sid, url=url, user=user, password_env=env))
                own = {"id", "kind", "url", "user", "password_env"}
        else:
            raise ConfigError(f"source {sid!r}: kind {kind!r} is not supported by this version")
        extra_sources[sid] = {k: v for k, v in raw.items() if k not in own}
    try:
        uid = check_uid(uid) if uid is not None else new_uid()
    except ValueError as error:
        raise ConfigError(f"`uid`: {error}") from error
    return Config(
        uid=uid,
        name=name,
        remote_rate=float(rate),
        public=public,
        admin_lan_only=lan_only,
        developer=developer,
        atlas=atlas,
        metrics=metrics,
        mqtt=mqtt,
        update_check=update_check,
        low_disk_mb=low_disk,
        webhook=webhook,
        sources=sources,
        _extra=extra,
        _extra_sources=extra_sources,
    )


def check(config: Config) -> Config:
    """The configuration as the file would give it back: raises ConfigError for what a reader would refuse
    (a bad address, two sources of one name…), and gives the values a reader normalises."""
    try:
        return _parse(tomllib.loads(config.to_toml()))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(str(error)) from error


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
