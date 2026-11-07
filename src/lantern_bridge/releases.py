# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""Homebrew from GitHub: the releases of the repositories the settings name, served as Lantern homebrew.

Each repository is read through GitHub's API, with the ETag of the last answer (a read that says "not modified"
costs no part of the rate limit; without a token GitHub allows 60 reads an hour, with one 5 000). A new release
gives a package: its `.nro` (or `.zip`, or the asset a pattern names), downloaded once, kept under the state
folder by its SHA-256, and described by what GitHub says of the repository and, when the release carries it, a
`lantern.json` asset (the manifest of the project: its id, name, kind, summary…). A zip's own manifest, the list
of the files it writes (SPEC.md § 8.2), is read from the archive.

Only public repositories are read, only addresses of GitHub are called, and an asset bigger than `MAX_ASSET` is
refused. What was served is kept in `homebrew/state.json` (`schema` 1; a file of a newer schema is read as
absent and never written over), so a reload or a restart serves it again without a download.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from lantern.models import Homebrew
from lantern.server.files import FileRef
from lantern.server.library import MemoryLibrary, make_package

log = logging.getLogger("lantern_bridge.releases")

SCHEMA = 1
API = "https://api.github.com"
CHECK = 6 * 3600.0  # how often a repository is read again
RETRY = 3600.0  # after a failure
MAX_ASSET = 256 << 20
MAX_MANIFEST = 64 << 10
KINDS = ("app", "game", "port", "emulator", "tool", "overlay", "sysmodule", "theme")
MANIFEST_NAME = "lantern.json"
_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_ID = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$")
_HOSTS = ("github.com", "githubusercontent.com")
_FIELDS = ("name", "kind", "summary", "description", "author", "license", "tags", "genres", "language")


class ReleaseError(Exception):
    """A repository cannot be served; said in a sentence for the page."""


def parse_repo(text: str) -> str:
    """`owner/name` from that, or from the address of the repository (`https://github.com/owner/name`)."""
    text = text.strip()
    if "://" in text:
        parts = urlsplit(text)
        if parts.scheme != "https" or (parts.hostname or "").lower() not in ("github.com", "www.github.com"):
            raise ReleaseError("Only a repository of github.com can be added.")
        text = parts.path.strip("/")
        segments = text.split("/")
        text = "/".join(segments[:2])
    text = text.removesuffix(".git")
    if not _REPO.match(text):
        raise ReleaseError("Give a repository as owner/name or its github.com address.")
    return text


def homebrew_id(repo: str) -> str:
    """A reverse-DNS id (SPEC.md § 8.1) for a repository: `com.github.<owner>.<name>`."""
    owner, name = repo.lower().split("/")

    def clean(part: str) -> str:
        return re.sub(r"[^a-z0-9_-]+", "-", part).strip("-_") or "x"

    return f"com.github.{clean(owner)}.{clean(name)}"


def pick_asset(assets: list[dict[str, Any]], pattern: str | None) -> dict[str, Any]:
    """The asset to serve: the one the pattern names, else the only `.nro`, else the only `.zip`."""
    named = [a for a in assets if isinstance(a.get("name"), str) and a.get("state", "uploaded") == "uploaded"]
    if pattern:
        found = [a for a in named if fnmatch.fnmatchcase(a["name"], pattern)]
        what = f"No asset of the release matches {pattern}."
    else:
        found = [a for a in named if a["name"].lower().endswith(".nro")]
        if not found:
            found = [a for a in named if a["name"].lower().endswith(".zip")]
        what = "The release has no .nro or .zip asset."
    if not found:
        raise ReleaseError(what)
    if len(found) > 1:
        raise ReleaseError("Several assets could be served: name the one to serve.")
    return found[0]


def clean_manifest(raw: object) -> dict[str, Any]:
    """The part of a project's `lantern.json` the bridge takes: known fields only, each of its own type."""
    if not isinstance(raw, dict):
        raise ReleaseError("lantern.json is not an object.")
    out: dict[str, Any] = {}
    ident = raw.get("id")
    if ident is not None:
        if not isinstance(ident, str) or not _ID.match(ident) or len(ident) > 128:
            raise ReleaseError("lantern.json: id is a reverse-DNS name such as com.example.tool.")
        out["id"] = ident
    for key in _FIELDS:
        value = raw.get(key)
        if value is None:
            continue
        if key in ("tags", "genres"):
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                raise ReleaseError(f"lantern.json: {key} is a list of words.")
            out[key] = value[:32]
        elif not isinstance(value, str) or not value.strip():
            raise ReleaseError(f"lantern.json: {key} is a text.")
        else:
            out[key] = value.strip()[:2000]
    if out.get("kind") not in (None, *KINDS):
        raise ReleaseError("lantern.json: kind is one of " + ", ".join(KINDS) + ".")
    return out


@dataclass(slots=True)
class Watch:
    """What is known of one repository."""

    repo: str
    etag: str = ""
    tag: str = ""
    asset: str = ""
    size: int = 0
    sha256: str = ""
    checked: float = 0.0
    next: float = 0.0  # when to read it again
    error: str = ""
    entry: dict[str, Any] | None = None  # the Homebrew served, as JSON
    file: str = ""  # its file, under the state folder

    def row(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "tag": self.tag,
            "asset": self.asset,
            "size": self.size,
            "checked": self.checked,
            "error": self.error,
            "served": self.entry is not None,
        }


@dataclass(slots=True)
class Releases:
    """Reads the repositories and serves what they release (one thread, started by the runtime)."""

    state_dir: Path
    library: MemoryLibrary
    repos: list[Any]  # config.HomebrewRepo
    token: str | None = None
    client: httpx.Client | None = None
    clock: Callable[[], float] = time.time
    watches: dict[str, Watch] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _wake: threading.Event = field(default_factory=threading.Event)
    _newer_schema: bool = False
    folder: Path = field(init=False)

    def __post_init__(self) -> None:
        self.folder = self.state_dir / "homebrew"
        if self.client is None:
            self.client = httpx.Client(
                timeout=30.0,
                follow_redirects=False,
                headers={
                    "User-Agent": "lantern-bridge",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        self._load()

    # -- what is kept -----------------------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self.folder / "state.json"

    def _load(self) -> None:
        """The watches of an earlier run, served again at once. A file of a newer schema is not read."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or not isinstance(data.get("repos"), dict):
            return
        if not isinstance(data.get("schema"), int) or data["schema"] > SCHEMA:
            self._newer_schema = True
            return
        for repo, row in data["repos"].items():
            if not isinstance(row, dict) or repo not in {r.repo for r in self.repos}:
                continue
            known = {k: row[k] for k in Watch.__slots__ if k in row and k != "repo"}
            try:
                self.watches[repo] = Watch(repo=repo, **known)
            except TypeError:
                continue
        for watch in self.watches.values():
            self._serve(watch)

    def _save(self) -> None:
        if self._newer_schema:
            return  # a file written by a newer bridge is left as it is
        rows = {w.repo: {k: getattr(w, k) for k in Watch.__slots__ if k != "repo"} for w in self.watches.values()}
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name("state.json.tmp")
            tmp.write_text(json.dumps({"schema": SCHEMA, "repos": rows}), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as error:
            log.warning("the homebrew state could not be written: %s", error.strerror or error)

    def _serve(self, watch: Watch) -> None:
        """Gives the library what is kept of a repository (nothing when its file is gone)."""
        if watch.entry is None or not watch.file or not Path(watch.file).is_file():
            watch.entry = None
            return
        try:
            entry = Homebrew.from_json(watch.entry)
            ref = FileRef(watch.file, watch.size, watch.sha256)
            self.library.add_homebrew(entry, {entry.package.file: ref})
        except (ValueError, KeyError, TypeError) as error:
            log.warning("homebrew %s cannot be served: %s", watch.repo, error)
            watch.error = f"It cannot be served: {error}"
            watch.entry = None

    # -- reading GitHub ---------------------------------------------------------------------------------

    def _get(self, url: str, etag: str = "") -> httpx.Response:
        headers = {"If-None-Match": etag} if etag else {}
        if self.token and url.startswith(API):
            headers["Authorization"] = f"Bearer {self.token}"
        reply = self.http.get(url, headers=headers)
        if reply.status_code in (403, 429) and reply.headers.get("x-ratelimit-remaining") == "0":
            reset = float(reply.headers.get("x-ratelimit-reset", "0") or 0)
            wait = max(60.0, reset - self.clock())
            raise ReleaseError(f"GitHub's rate limit is reached: tried again in {int(wait // 60)} minutes.")
        return reply

    def _download(self, url: str, limit: int) -> bytes:
        """An asset: only addresses of GitHub, at most three redirects, at most `limit` bytes."""
        for _ in range(4):
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            if parts.scheme != "https" or not any(host == h or host.endswith("." + h) for h in _HOSTS):
                raise ReleaseError("The asset is not on GitHub: it is not downloaded.")
            with self.http.stream("GET", url) as reply:
                if reply.is_redirect:
                    url = str(reply.headers.get("location", ""))
                    continue
                if reply.status_code != 200:
                    raise ReleaseError(f"GitHub answered {reply.status_code} for the asset.")
                data = bytearray()
                for chunk in reply.iter_bytes():
                    data += chunk
                    if len(data) > limit:
                        raise ReleaseError("The asset is bigger than the bridge takes.")
                return bytes(data)
        raise ReleaseError("Too many redirects for the asset.")

    def check(self, repo: Any, *, force: bool = False) -> Watch:
        """Reads one repository now; what went wrong is in `error`, never raised."""
        watch = self.watches.setdefault(repo.repo, Watch(repo=repo.repo))
        now = self.clock()
        try:
            self._read(repo, watch, force)
            watch.error = ""
            watch.next = now + CHECK
        except ReleaseError as error:
            watch.error = str(error)
            watch.next = now + RETRY
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as error:
            log.warning("homebrew %s: %s", repo.repo, error)
            watch.error = "GitHub could not be read or the answer was not understood."
            watch.next = now + RETRY
        watch.checked = now
        with self._lock:
            self._save()
        return watch

    def _read(self, repo: Any, watch: Watch, force: bool) -> None:
        url = f"{API}/repos/{repo.repo}/releases" + ("?per_page=10" if repo.prerelease else "/latest")
        reply = self._get(url, "" if force or watch.entry is None else watch.etag)
        if reply.status_code == 304:
            return
        if reply.status_code == 404:
            raise ReleaseError("No release was found: is the repository public, and does it have one?")
        if reply.status_code != 200:
            raise ReleaseError(f"GitHub answered {reply.status_code}.")
        body = reply.json()
        if repo.prerelease:
            body = next((r for r in body if isinstance(r, dict) and not r.get("draft")), None)
        if not isinstance(body, dict) or body.get("draft"):
            raise ReleaseError("No release was found.")
        etag = reply.headers.get("etag", "")
        tag = str(body.get("tag_name", ""))
        assets = [a for a in body.get("assets", []) if isinstance(a, dict)]
        asset = pick_asset(assets, repo.asset)
        if watch.entry is not None and watch.tag == tag and watch.asset == asset["name"] and not force:
            watch.etag = etag
            return
        size = int(asset.get("size", 0))
        if not 0 < size <= MAX_ASSET:
            raise ReleaseError("The asset is empty or bigger than the bridge takes.")
        data = self._download(str(asset.get("browser_download_url", "")), MAX_ASSET)
        manifest = self._manifest(assets)
        about = self._about(repo.repo)
        entry, ref = self._entry(repo, body, asset, data, manifest=manifest, about=about)
        cached = self._keep(data, ref.sha256)
        library_ref = FileRef(str(cached), ref.size, ref.sha256)
        try:
            self.library.add_homebrew(entry, {entry.package.file: library_ref})
        except ValueError as error:  # what the specification forbids: a path outside the roots of the platform…
            raise ReleaseError(f"The library refuses it: {error}") from error
        old = watch.file
        watch.etag, watch.tag, watch.asset = etag, tag, asset["name"]
        watch.size, watch.sha256, watch.file = ref.size, ref.sha256, str(cached)
        watch.entry = entry.to_json()
        if old and old != watch.file:
            Path(old).unlink(missing_ok=True)

    def _manifest(self, assets: list[dict[str, Any]]) -> dict[str, Any]:
        found = next((a for a in assets if a.get("name") == MANIFEST_NAME), None)
        if found is None:
            return {}
        if int(found.get("size", 0)) > MAX_MANIFEST:
            raise ReleaseError("lantern.json is too big.")
        try:
            raw = json.loads(self._download(str(found.get("browser_download_url", "")), MAX_MANIFEST))
        except ValueError as error:
            raise ReleaseError("lantern.json is not valid JSON.") from error
        return clean_manifest(raw)

    def _about(self, repo: str) -> dict[str, Any]:
        """What GitHub says of the repository: description, licence, topics, owner. Not needed to serve it."""
        try:
            reply = self._get(f"{API}/repos/{repo}")
            return reply.json() if reply.status_code == 200 and isinstance(reply.json(), dict) else {}
        except (httpx.HTTPError, ValueError):
            return {}

    def _entry(
        self,
        repo: Any,
        release: dict[str, Any],
        asset: dict[str, Any],
        data: bytes,
        *,
        manifest: dict[str, Any],
        about: dict[str, Any],
    ) -> tuple[Homebrew, FileRef]:
        ident = manifest.get("id") or homebrew_id(repo.repo)
        name_of_file = str(asset["name"])
        fmt = "nro" if name_of_file.lower().endswith(".nro") else "zip"
        package, ref = make_package(data, fmt, platform="switch", homebrew_id=ident, file_name=name_of_file)
        published = str(release.get("published_at") or release.get("created_at") or "")
        try:
            build = int(datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp())
        except ValueError:
            build = int(self.clock())
        licence = (about.get("license") or {}).get("spdx_id") if isinstance(about.get("license"), dict) else None
        topics = [t for t in about.get("topics", []) if isinstance(t, str)][:32] if about.get("topics") else []
        owner = (about.get("owner") or {}).get("login") if isinstance(about.get("owner"), dict) else None
        raw: dict[str, Any] = {
            "schema": 1,
            "id": ident,
            "platform": "switch",
            "kind": manifest.get("kind") or repo.kind or "app",
            "name": manifest.get("name") or repo.repo.split("/")[1],
            "author": manifest.get("author") or owner or repo.repo.split("/")[0],
            "version": str(release.get("tag_name", "")).removeprefix("v") or "0",
            "build": build,
            "source": f"https://github.com/{repo.repo}",
            "summary": manifest.get("summary") or about.get("description"),
            "description": manifest.get("description") or (release.get("body") or None),
            "license": manifest.get("license") or (licence if licence not in (None, "NOASSERTION") else None),
            "tags": manifest.get("tags") or topics or None,
            "genres": manifest.get("genres"),
            "language": manifest.get("language"),
            "updated": published or None,
            "package": package.to_json(),
        }
        entry = Homebrew.from_json({k: v for k, v in raw.items() if v not in (None, "", [])})
        return entry, ref

    def _keep(self, data: bytes, sha256: str) -> Path:
        files = self.folder / "files"
        files.mkdir(parents=True, exist_ok=True)
        target = files / sha256
        if not target.exists():
            tmp = files / f".{sha256}.tmp"
            tmp.write_bytes(data)
            os.replace(tmp, target)
        return target

    # -- the thread -------------------------------------------------------------------------------------

    def wake(self) -> None:
        self._wake.set()

    def run(self, stop: threading.Event) -> None:
        """Reads each repository when its time comes (and at once when the settings add one)."""
        while not stop.is_set():
            now = self.clock()
            for repo in list(self.repos):
                if stop.is_set():
                    break
                watch = self.watches.get(repo.repo)
                if watch is None or watch.next <= now:
                    self.check(repo)
            self._wake.wait(60)
            self._wake.clear()

    def status(self) -> list[dict[str, Any]]:
        """One row per repository of the settings, for the page."""
        return [(self.watches.get(r.repo) or Watch(repo=r.repo)).row() for r in self.repos]

    @property
    def http(self) -> httpx.Client:
        if self.client is None:  # made in __post_init__
            raise RuntimeError("no client")
        return self.client

    def close(self) -> None:
        self.http.close()
