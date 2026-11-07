# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The library as a person browses it: titles with their versions and files, from the index.

The index knows every file (hashed or still waiting); the projection knows which of them the server
offers. This module groups them by title id for the administration pages, and keeps no state of its own
but a few seconds of cache.
"""

from __future__ import annotations

import csv
import io
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from lantern_bridge.identify import identify
from lantern_bridge.index import Index
from lantern_bridge.library import Projection

TYPES = ("base", "update", "dlc")


@dataclass(frozen=True, slots=True)
class FileInfo:
    source: str
    path: str
    name: str
    size: int
    sha256: str | None
    type: str
    version: int
    app_id: str
    format: str
    served: bool  # this file is the one the server offers for its bytes


@dataclass(slots=True)
class TitleInfo:
    title_id: str
    name: str
    files: list[FileInfo] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)

    @property
    def types(self) -> set[str]:
        return {f.type for f in self.files}

    @property
    def sources(self) -> list[str]:
        return sorted({f.source for f in self.files})

    @property
    def size(self) -> int:
        """What the server offers of the title: each distinct content once."""
        seen: dict[str, int] = {}
        for f in self.files:
            seen.setdefault(f.sha256 or f"{f.source}/{f.path}", f.size)
        return sum(seen.values())

    @property
    def pending(self) -> int:
        return sum(1 for f in self.files if f.sha256 is None)

    @property
    def hashed(self) -> int:
        return len(self.files) - self.pending

    @property
    def latest(self) -> int:
        return max((f.version for f in self.files if f.type in ("base", "update")), default=0)

    def count(self, kind: str) -> int:
        return sum(1 for f in self.files if f.type == kind)

    @property
    def initials(self) -> str:
        words = [w for w in self.name.replace("-", " ").split() if w[:1].isalnum()]
        return "".join(w[0] for w in words[:2]).upper() or "?"

    @property
    def tone(self) -> int:
        """One of twelve colours of the aurora's range, the same for a title every time (its id decides)."""
        return int(self.title_id[-8:-3] or "0", 16) % 12


@dataclass(frozen=True, slots=True)
class Issue:
    """Something worth a look in the library: `kind` is one of KINDS, `detail` says which files."""

    kind: str
    title_id: str
    name: str
    detail: str
    #: For a duplicate: which of the files the library serves (the others are not offered), as in `detail`.
    served: str = ""


#: same_content: the same bytes twice; same_version: two different files for one version; no_base: an
#: update or DLC whose game is not in the library.
KINDS = ("same_content", "same_version", "no_base")


@dataclass(slots=True)
class Catalog:
    titles: dict[str, TitleInfo]

    def export(self, generated: str) -> dict[str, object]:
        """The whole library as a document (`schema` 1): the titles, and for each its files."""
        return {
            "schema": EXPORT_SCHEMA,
            "generated": generated,
            "titles": [
                {
                    "title_id": t.title_id,
                    "name": t.name,
                    "genres": t.genres,
                    "files": [
                        {
                            "type": f.type,
                            "app_id": f.app_id,
                            "version": f.version,
                            "format": f.format,
                            "size": f.size,
                            "sha256": f.sha256,
                            "source": f.source,
                            "path": f.path.rsplit("#", 1)[-1],
                        }
                        for f in t.files
                    ],
                }
                for t in sorted(self.titles.values(), key=lambda t: (t.name.lower(), t.title_id))
            ],
        }

    def export_csv(self) -> str:
        """One line a file, for a spreadsheet."""
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(_CSV_COLUMNS)
        for t in sorted(self.titles.values(), key=lambda t: (t.name.lower(), t.title_id)):
            for f in t.files:
                row = (t.title_id, t.name, f.type, f.app_id, f.version, f.format, f.size, f.sha256, f.source)
                writer.writerow([*(_cell(v) for v in row), _cell(f.path.rsplit("#", 1)[-1])])
        return out.getvalue()

    def issues(self) -> list[Issue]:
        """Duplicates and orphans, found by comparing what the index lists (nothing is read from disk)."""
        found: list[Issue] = []
        for title in self.titles.values():
            if title.count("base") == 0:
                found.append(
                    Issue("no_base", title.title_id, title.name, ", ".join(sorted({f.name for f in title.files})))
                )
            groups: dict[tuple[str, str, int], list[FileInfo]] = {}
            for f in title.files:
                groups.setdefault((f.type, f.app_id, f.version), []).append(f)
            for same in groups.values():
                if len(same) < 2:
                    continue
                hashes = {f.sha256 for f in same}
                kind = "same_content" if len(hashes) == 1 and None not in hashes else "same_version"

                def label(f: FileInfo) -> str:
                    return f"{f.source}: {f.path.rsplit('#', 1)[-1]}"

                found.append(
                    Issue(
                        kind,
                        title.title_id,
                        title.name,
                        ", ".join(label(f) for f in same),
                        ", ".join(label(f) for f in same if f.served),
                    )
                )
        return sorted(found, key=lambda i: (KINDS.index(i.kind), i.name.lower()))

    def genres(self) -> list[str]:
        """Every genre a title of the library has, in alphabetical order."""
        return sorted({g for t in self.titles.values() for g in t.genres})

    def counts(self) -> dict[str, int]:
        out = {"all": len(self.titles), **dict.fromkeys(TYPES, 0)}
        for title in self.titles.values():
            for kind in title.types:
                out[kind] += 1
        return out


#: The version of the exported document (see Catalog.export).
EXPORT_SCHEMA = 1
_CSV_COLUMNS = ("title_id", "title", "type", "app_id", "version", "format", "size", "sha256", "source", "path")


def _cell(value: object) -> str:
    """A spreadsheet reads a cell that starts with = + - @ as a formula: a quote in front makes it text."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def build(index: Index, projection: Projection) -> Catalog:
    titles: dict[str, TitleInfo] = {}
    bases: dict[str, str] = {}
    for row in index.rows():
        identity = identify(row.path)
        if identity is None:  # the scan keeps only what the names identify; a leftover row is ignored
            continue
        title = titles.setdefault(identity.title_id, TitleInfo(identity.title_id, identity.name))
        if identity.type == "base":
            bases[identity.title_id] = identity.name
        title.files.append(
            FileInfo(
                source=row.source,
                path=row.path,
                name=row.path.rsplit("#", 1)[-1].rsplit("/", 1)[-1],
                size=row.size,
                sha256=row.sha256,
                type=identity.type,
                version=identity.version_code,
                app_id=identity.app_id,
                format=identity.format,
                served=projection.serving(row),
            )
        )
    for title_id, title in titles.items():
        sheet = projection.title(title_id)
        title.name = (sheet.name if sheet is not None and sheet.name else None) or bases.get(title_id) or title.name
        title.genres = list(sheet.genres or []) if sheet is not None else []
        title.files.sort(key=lambda f: (TYPES.index(f.type), f.version, f.source, f.path))
    return Catalog(titles)


class Browser:
    """A short-lived cache of the catalogue: the pages ask for it often, the files change slowly."""

    def __init__(self, index: Index, projection: Projection, ttl: float = 3.0) -> None:
        self.index, self.projection, self.ttl = index, projection, ttl
        self._lock = threading.Lock()
        self._built = 0.0
        self._catalog: Catalog | None = None

    def catalog(self) -> Catalog:
        with self._lock:
            now = time.monotonic()
            if self._catalog is None or now - self._built > self.ttl:
                self._catalog = build(self.index, self.projection)
                self._built = now
            return self._catalog

    def forget(self) -> None:
        with self._lock:
            self._catalog = None


SORTS = ("name", "size", "version")
PAGE = 48


@dataclass(frozen=True, slots=True)
class Page:
    titles: list[TitleInfo]
    total: int
    page: int
    pages: int


def search(
    catalog: Catalog,
    *,
    q: str = "",
    kind: str = "",
    source: str = "",
    state: str = "",
    genre: str = "",
    sort: str = "name",
    page: int = 1,
    per_page: int = PAGE,
) -> Page:
    """Filter, sort and cut the titles; an unknown value of a filter is no filter."""
    needle = q.strip().lower()

    def keep(title: TitleInfo) -> bool:
        if needle and needle not in title.name.lower() and needle not in title.title_id.lower():
            return False
        if kind in TYPES and kind not in title.types:
            return False
        if source and source not in title.sources:
            return False
        if genre and genre not in title.genres:
            return False
        if state == "pending" and not title.pending:
            return False
        return not (state == "hashed" and title.pending)

    found: Iterable[TitleInfo] = (t for t in catalog.titles.values() if keep(t))
    if sort == "size":
        ordered = sorted(found, key=lambda t: (-t.size, t.name.lower()))
    elif sort == "version":
        ordered = sorted(found, key=lambda t: (-t.latest, t.name.lower()))
    else:
        ordered = sorted(found, key=lambda t: (t.name.lower(), t.title_id))
    pages = max(1, -(-len(ordered) // per_page))
    page = min(max(1, page), pages)
    return Page(ordered[(page - 1) * per_page : page * per_page], len(ordered), page, pages)
