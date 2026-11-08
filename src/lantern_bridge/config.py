# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The configuration file: TOML, versioned by an integer ``schema``.

A reader skips what it does not know and keeps it when it writes the file
back. A file written by a newer schema is refused and never rewritten.
"""

from __future__ import annotations

import os
import re
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
    #: Off unless the settings say so: the bridge then never writes in the folder (a read-only volume is fine).
    #: On, the pages may delete a file of it and compress it.
    writable: bool = False

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
    #: The environment variable that holds the shop's host-verification value (the `Hauth` header Tinfoil
    #: sends); only a shop that checks its host (behind HTTPS) needs it.
    hauth_env: str | None = None

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
class Oidc:
    """An OpenID Connect provider the pages sign in with (authorization code with PKCE)."""

    issuer: str
    client_id: str
    #: Where the browser comes back to: this bridge's address as the browser sees it, no path.
    public_url: str
    #: The environment variable holding the client secret; None for a public client (PKCE alone).
    secret_env: str | None = None
    name_claim: str = "preferred_username"
    groups_claim: str = "groups"
    scopes: str = "openid profile email groups"
    #: The provider's name on the sign-in button; empty: a generic text.
    label: str = ""


@dataclass(slots=True)
class SignIn:
    """How the administration pages let people in besides the operators' own passwords (`[sign_in]`).

    A name (from the proxy's header, or the provider) and its groups become a role through `roles`; nobody whose
    groups match none gets in. Everything read is kept in `table` to be written back whole: the pages do not edit it.
    """

    local: bool = True
    logout_url: str | None = None
    roles: dict[str, str] = field(default_factory=dict)
    #: The headers a trusted proxy puts its user and groups in; None: the proxy mode is off.
    proxy_user_header: str | None = None
    proxy_groups_header: str = "Remote-Groups"
    oidc: Oidc | None = None
    table: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def external(self) -> bool:
        return self.proxy_user_header is not None or self.oidc is not None


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


@dataclass(frozen=True, slots=True)
class HomebrewRepo:
    """A GitHub repository whose releases are served as homebrew (see releases.py)."""

    repo: str  # owner/name
    asset: str | None = None  # a pattern naming the asset to serve, when the release has several
    kind: str | None = None  # app, game, port, emulator, tool, overlay, sysmodule or theme
    prerelease: bool = False  # True: the latest release counts even when it is marked as a pre-release


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
    # Networks (`192.168.1.0/24`) the administration pages answer to, instead of every private address. Empty: the
    # private and loopback addresses. Loopback always may.
    admin_allow: list[str] = field(default_factory=list)
    # Names the administration pages answer to (the `Host` header). Empty: addresses, one-word names and local ones.
    admin_hosts: list[str] = field(default_factory=list)
    # Reverse proxies whose `X-Forwarded-For` and `X-Forwarded-Proto` are believed, as networks.
    trusted_proxies: list[str] = field(default_factory=list)
    mqtt: Mqtt | None = None
    # Sign-in through a trusted proxy or an OpenID Connect provider; None: the operators' passwords only.
    sign_in: SignIn | None = None
    # Megabytes free on the volume of the state below which the health says to look (it fails below 20).
    low_disk_mb: int = 500
    # Files hashed at once, from the folders (0: the machine decides, see `hashing.workers`). Hashing is the SHA-256
    # of OpenSSL, which already uses the processor's own instructions; more files at once use more cores.
    hash_threads: int = 0
    # Threads nsz compresses with (0: its own choice, every logical core for a block compression).
    compress_threads: int = 0
    # True: the bridge reads GitHub's latest release once a day and says on the overview when newer. On by default.
    update_check: bool = True
    # An address that gets a POST when scans found changes (a Home Assistant webhook, ntfy, any tool). None: no call.
    webhook: str | None = None
    # An address that gets a short text when something needs a person (a source down or back, a newer release): an
    # ntfy topic, a Gotify or Home Assistant notify hook, any tool that takes a POST. None: no call.
    notify: str | None = None
    sources: list[Source] = field(default_factory=list)
    # GitHub repositories whose releases are served as homebrew, and the environment variable holding a GitHub token
    # (optional: without it GitHub allows 60 reads an hour, with it 5 000).
    homebrew: list[HomebrewRepo] = field(default_factory=list)
    github_token_env: str | None = None
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
        if self.hash_threads:
            data["hash_threads"] = self.hash_threads
        if self.compress_threads:
            data["compress_threads"] = self.compress_threads
        for key in ("admin_allow", "admin_hosts", "trusted_proxies"):
            if getattr(self, key):
                data[key] = list(getattr(self, key))
        if self.mqtt is not None:
            data["mqtt"] = self.mqtt.table()
        if self.sign_in is not None:
            data["sign_in"] = deepcopy(self.sign_in.table)
        if self.webhook is not None:
            data["webhook"] = self.webhook
        if self.notify is not None:
            data["notify"] = self.notify
        if self.github_token_env is not None:
            data["github_token_env"] = self.github_token_env
        if self.homebrew:
            data["homebrew"] = [
                {
                    "repo": r.repo,
                    **({"asset": r.asset} if r.asset else {}),
                    **({"kind": r.kind} if r.kind else {}),
                    **({"prerelease": True} if r.prerelease else {}),
                }
                for r in self.homebrew
            ]
        data["sources"] = [{**self._extra_sources.get(s.id, {}), **_source_table(s)} for s in self.sources]
        return tomli_w.dumps(data)


def _source_table(source: Source) -> dict[str, Any]:
    if isinstance(source, FolderSource):
        folder: dict[str, Any] = {"id": source.id, "kind": "folder", "path": str(source.path)}
        if source.writable:
            folder["writable"] = True
        return folder
    kind = {OwnfoilSource: "ownfoil", AerofoilSource: "aerofoil"}.get(type(source), "tinfoil")
    table: dict[str, Any] = {"id": source.id, "kind": kind, "url": source.url}
    if source.user is not None:
        table["user"] = source.user
    if source.password_env is not None:
        table["password_env"] = source.password_env
    if source.hauth_env is not None:
        table["hauth_env"] = source.hauth_env
    if isinstance(source, AerofoilSource) and source.logins:
        table["logins"] = [
            {"account": x.account, "user": x.user, "password_env": x.password_env} for x in source.logins
        ]
    return table


def parse_list(raw: object, key: str, *, networks: bool = False) -> list[str]:
    """A list of texts: names, or (with `networks`) addresses and networks such as `192.168.1.0/24`."""
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        raise ConfigError(f"`{key}` must be a list of texts")
    items = [item.strip() for item in raw]
    if networks:
        from ipaddress import ip_network

        for item in items:
            try:
                ip_network(item, strict=False)
            except ValueError as error:
                raise ConfigError(f"`{key}`: {item!r} is not an address or a network (like 192.168.1.0/24)") from error
    return items


def parse_webhook(raw: object, key: str = "webhook") -> str | None:
    """The address that gets a POST, or None (an empty text is none)."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ConfigError(f"`{key}` must be an address")
    parsed = urlsplit(raw.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError(f"`{key}` must be an http or https address")
    if parsed.username or parsed.password:
        raise ConfigError(f"no credentials in the `{key}` address: put a secret in its path if the service wants one")
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


_HEADER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_CLAIM = re.compile(r"^[A-Za-z0-9_:./-]{1,64}$")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _text(table: dict[str, Any], key: str, where: str, default: str | None = None) -> str | None:
    value = table.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"`{where}.{key}` must be a non-empty string")
    return value.strip()


def _https_url(value: str, name: str, *, path_ok: bool) -> str:
    """An https address (http only for the machine itself, for a test); no query, no fragment."""
    parts = urlsplit(value)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme not in ("https", "http") or not parts.hostname or (parts.scheme == "http" and not local):
        raise ConfigError(f"`{name}` must be an https address")
    if parts.query or parts.fragment or parts.username or parts.password or (parts.path.strip("/") and not path_ok):
        raise ConfigError(f"`{name}` must not carry a query, a fragment, a login{'' if path_ok else ' or a path'}")
    return value.rstrip("/")


def parse_sign_in(raw: object, trusted_proxies: list[str]) -> SignIn | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("`sign_in` must be a table")
    from lantern_bridge.admin.auth import ROLES  # at call time: the pages import this module

    local = raw.get("local", True)
    if not isinstance(local, bool):
        raise ConfigError("`sign_in.local` must be true or false")
    logout = _text(raw, "logout_url", "sign_in")
    if logout is not None:
        logout = _https_url(logout, "sign_in.logout_url", path_ok=True)
    roles = raw.get("roles", {})
    if not isinstance(roles, dict) or any(not isinstance(k, str) or not k or v not in ROLES for k, v in roles.items()):
        raise ConfigError(f"`sign_in.roles` maps a group name to one of {', '.join(ROLES)}")
    sign_in = SignIn(local=local, logout_url=logout, roles=dict(roles), table=deepcopy(raw))
    proxy = raw.get("proxy")
    if proxy is not None:
        if not isinstance(proxy, dict) or not isinstance(proxy.get("enabled", False), bool):
            raise ConfigError("`sign_in.proxy` must be a table with `enabled` true or false")
        if proxy.get("enabled", False):
            user, groups = (
                _text(proxy, "user_header", "sign_in.proxy", "Remote-User"),
                _text(proxy, "groups_header", "sign_in.proxy", "Remote-Groups"),
            )
            if not (user and groups and _HEADER.match(user) and _HEADER.match(groups)):
                raise ConfigError("`sign_in.proxy` headers must be header names")
            if not trusted_proxies:
                raise ConfigError("`sign_in.proxy` needs `trusted_proxies`: the headers are only believed from them")
            sign_in.proxy_user_header, sign_in.proxy_groups_header = user, groups
    oidc = raw.get("oidc")
    if oidc is not None:
        if not isinstance(oidc, dict) or not isinstance(oidc.get("enabled", False), bool):
            raise ConfigError("`sign_in.oidc` must be a table with `enabled` true or false")
        if oidc.get("enabled", False):
            issuer, client_id = _text(oidc, "issuer", "sign_in.oidc"), _text(oidc, "client_id", "sign_in.oidc")
            public_url = _text(oidc, "public_url", "sign_in.oidc")
            if not (issuer and client_id and public_url):
                raise ConfigError("`sign_in.oidc` needs `issuer`, `client_id` and `public_url`")
            secret = _text(oidc, "secret_env", "sign_in.oidc")
            if secret is not None and not _ENV.match(secret):
                raise ConfigError("`sign_in.oidc.secret_env` must name an environment variable")
            name_claim = _text(oidc, "name_claim", "sign_in.oidc", "preferred_username") or ""
            groups_claim = _text(oidc, "groups_claim", "sign_in.oidc", "groups") or ""
            scopes = _text(oidc, "scopes", "sign_in.oidc", "openid profile email groups") or ""
            if not (_CLAIM.match(name_claim) and _CLAIM.match(groups_claim)) or "openid" not in scopes.split():
                raise ConfigError("`sign_in.oidc` claims are names, and `scopes` holds `openid`")
            sign_in.oidc = Oidc(
                _https_url(issuer, "sign_in.oidc.issuer", path_ok=True),
                client_id,
                _https_url(public_url, "sign_in.oidc.public_url", path_ok=False),
                secret,
                name_claim,
                groups_claim,
                scopes,
                ((_text(oidc, "label", "sign_in.oidc") if "label" in oidc else None) or "")[:40],
            )
    if sign_in.external and not roles:
        raise ConfigError("`sign_in.roles` must name the groups that may sign in")
    if not local and not sign_in.external:
        raise ConfigError("`sign_in.local = false` needs the proxy or OIDC on: nobody could sign in")
    return sign_in


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
        "hash_threads",
        "compress_threads",
        "homebrew",
        "github_token_env",
        "admin_allow",
        "admin_hosts",
        "trusted_proxies",
        "mqtt",
        "sign_in",
        "webhook",
        "notify",
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
    threads: dict[str, int] = {}
    for key in ("hash_threads", "compress_threads"):
        count = data.get(key, 0)
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 64:
            raise ConfigError(f"`{key}` must be a number of threads from 0 (automatic) to 64")
        threads[key] = count
    homebrew = _homebrew(data.get("homebrew"))
    token_env = data.get("github_token_env")
    if token_env is not None and (not isinstance(token_env, str) or not token_env.strip()):
        raise ConfigError("`github_token_env` must name an environment variable")
    update_check = data.get("update_check", True)
    if not isinstance(update_check, bool):
        raise ConfigError("`update_check` must be true or false")
    admin_allow = parse_list(data.get("admin_allow"), "admin_allow", networks=True)
    admin_hosts = parse_list(data.get("admin_hosts"), "admin_hosts")
    trusted_proxies = parse_list(data.get("trusted_proxies"), "trusted_proxies", networks=True)
    mqtt = parse_mqtt(data.get("mqtt"))
    sign_in = parse_sign_in(data.get("sign_in"), trusted_proxies)
    webhook = parse_webhook(data.get("webhook"))
    notify = parse_webhook(data.get("notify"), "notify")
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
            writable = raw.get("writable", False)
            if not isinstance(writable, bool):
                raise ConfigError(f"source {sid!r}: `writable` must be true or false")
            sources.append(FolderSource(id=sid, path=Path(path), writable=writable))
            own = {"id", "kind", "path", "writable"}
        elif kind in ("tinfoil", "ownfoil", "aerofoil"):
            user, env = raw.get("user"), raw.get("password_env")
            for key, value in (("user", user), ("password_env", env)):
                if value is not None and (not isinstance(value, str) or not value):
                    raise ConfigError(f"source {sid!r}: `{key}` must be a non-empty string")
            if (user is None) != (env is None):
                raise ConfigError(f"source {sid!r}: `user` and `password_env` go together")
            hauth = raw.get("hauth_env")
            if hauth is not None and (not isinstance(hauth, str) or not hauth):
                raise ConfigError(f"source {sid!r}: `hauth_env` must be a non-empty string")
            url = _parse_url(sid, raw.get("url"))
            if kind == "aerofoil":
                sources.append(
                    AerofoilSource(
                        id=sid, url=url, user=user, password_env=env, hauth_env=hauth, logins=_logins(sid, raw)
                    )
                )
                own = {"id", "kind", "url", "user", "password_env", "hauth_env", "logins"}
            else:
                shop = OwnfoilSource if kind == "ownfoil" else TinfoilSource
                sources.append(shop(id=sid, url=url, user=user, password_env=env, hauth_env=hauth))
                own = {"id", "kind", "url", "user", "password_env", "hauth_env"}
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
        sign_in=sign_in,
        update_check=update_check,
        low_disk_mb=low_disk,
        homebrew=homebrew,
        github_token_env=token_env.strip() if isinstance(token_env, str) else None,
        hash_threads=threads["hash_threads"],
        compress_threads=threads["compress_threads"],
        admin_allow=admin_allow,
        admin_hosts=admin_hosts,
        trusted_proxies=trusted_proxies,
        webhook=webhook,
        notify=notify,
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


def _homebrew(raw: object) -> list[HomebrewRepo]:
    from lantern_bridge.releases import KINDS, ReleaseError, parse_repo

    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(row, dict) for row in raw):
        raise ConfigError("`homebrew` must be a list of tables (`[[homebrew]]`)")
    repos: list[HomebrewRepo] = []
    for row in raw:
        try:
            name = parse_repo(str(row.get("repo", "")))
        except ReleaseError as error:
            raise ConfigError(f"homebrew: {error}") from error
        if name.lower() in {r.repo.lower() for r in repos}:
            raise ConfigError(f"homebrew: {name} is there twice")
        asset, kind, pre = row.get("asset"), row.get("kind"), row.get("prerelease", False)
        if asset is not None and (not isinstance(asset, str) or not asset.strip() or len(asset) > 200):
            raise ConfigError(f"homebrew {name}: `asset` must be a name or a pattern such as `*.nro`")
        if kind is not None and kind not in KINDS:
            raise ConfigError(f"homebrew {name}: `kind` must be one of {', '.join(KINDS)}")
        if not isinstance(pre, bool):
            raise ConfigError(f"homebrew {name}: `prerelease` must be true or false")
        repos.append(HomebrewRepo(name, asset.strip() if asset else None, kind, pre))
    return repos


def parse_text(text: str, uid: str | None = None) -> Config:
    """Settings given as text (an exported file): ConfigError for what a reader would refuse.

    With `uid`, the file's own identity is ignored and this one is used (a bridge keeps its own).
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"not valid TOML ({error})") from error
    if uid is not None:
        data["uid"] = uid
    return _parse(data)


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


def _flat(config: Config) -> dict[str, Any]:
    """The settings as `key: value` lines of one level, the sources under their id (`sources.shop.url`)."""
    data = tomllib.loads(config.to_toml())
    flat: dict[str, Any] = {}
    for key, value in data.items():
        if key == "sources":
            for row in value:
                for name, item in row.items():
                    flat[f"sources.{row.get('id')}.{name}"] = item
        elif isinstance(value, dict):
            for name, item in value.items():
                flat[f"{key}.{name}"] = item
        else:
            flat[key] = value
    return flat


def changes(before: Config, after: Config) -> list[str]:
    """What differs between two settings, for the journal: the names of the settings that changed.

    A switch or a number says what it went from and to (``public: false -> true``); a text (an address, a path, a
    name) is only named, and so is a source that comes or goes, because a value may hold what is private.
    """
    old, new = _flat(before), _flat(after)
    added = {k.split(".")[1] for k in new if k.startswith("sources.")} - {
        k.split(".")[1] for k in old if k.startswith("sources.")
    }
    gone = {k.split(".")[1] for k in old if k.startswith("sources.")} - {
        k.split(".")[1] for k in new if k.startswith("sources.")
    }
    lines = [f"sources.{sid} added" for sid in sorted(added)] + [f"sources.{sid} removed" for sid in sorted(gone)]
    for key in sorted(old.keys() | new.keys()):
        parts = key.split(".")
        if key == "schema" or (parts[0] == "sources" and parts[1] in added | gone):
            continue
        if old.get(key) == new.get(key):
            continue
        a, b = old.get(key), new.get(key)
        scalar = all(isinstance(x, bool | int | float) for x in (a, b) if x is not None)
        if scalar and a is not None and b is not None:
            lines.append(f"{key}: {str(a).lower()} -> {str(b).lower()}")
        else:
            lines.append(key)
    return lines
