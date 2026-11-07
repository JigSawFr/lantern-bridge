# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""Local hashing: the packages of the folders, queued by what the names say. Reading and recording the hashes is
the SDK's (``lantern.server.file_index``)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from lantern.server.file_index import Entry, scan

from lantern_bridge.identify import identify

#: What looks like a Switch package but that the name does not identify is reported, a few at most
#: (the pages say why a file is missing from the library).
IGNORED_LIMIT = 500
_PACKAGE_SUFFIXES = (".nsp", ".nsz", ".xci", ".xcz")


def scan_packages(root: Path, ignored: list[tuple[str, int]] | None = None) -> Iterator[Entry]:
    """The Switch packages under ``root`` the names identify, with their queue priority.

    Other files are not hashed: nothing would list them. A package the name does not identify is appended to
    `ignored` as (path, size) when that list is given.
    """
    for entry in scan(root):
        identity = identify(entry.path)
        if identity is not None:
            yield Entry(entry.path, entry.size, entry.mtime_ns, identity.priority)
        elif ignored is not None and entry.path.lower().endswith(_PACKAGE_SUFFIXES) and len(ignored) < IGNORED_LIMIT:
            ignored.append((entry.path, entry.size))
