# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""Local hashing: read the files in place and record their SHA-256."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

from lantern_bridge.index import Entry, Index

_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def scan(root: Path) -> Iterator[Entry]:
    """The regular files under ``root``, paths relative with ``/``.

    Symbolic links are not followed: a link could point out of the volume.
    """
    for directory, subdirectories, names in os.walk(root, followlinks=False):
        subdirectories[:] = sorted(d for d in subdirectories if not (Path(directory) / d).is_symlink())
        for name in sorted(names):
            file = Path(directory) / name
            if file.is_symlink():
                continue
            info = file.stat()
            yield Entry(path=file.relative_to(root).as_posix(), size=info.st_size, mtime_ns=info.st_mtime_ns)


def hash_pending(
    index: Index,
    roots: Mapping[str, Path],
    *,
    limit: int = 100,
    on_hashed: Callable[[str, str], None] | None = None,
) -> int:
    """Hash up to ``limit`` pending files; returns how many were recorded.

    A file that changed or vanished while it was read is left pending (or
    removed by the next ``sync``): the hash is kept only if the validators
    still match.
    """
    done = 0
    for row in index.pending(limit):
        root = roots.get(row.source)
        if root is None:
            continue
        file = root / row.path
        try:
            before = file.stat()
            if (before.st_size, before.st_mtime_ns) != (row.size, row.mtime_ns):
                continue
            digest = sha256_file(file)
        except OSError:
            continue
        if index.set_hash(row, digest):
            done += 1
            if on_hashed is not None:
                on_hashed(row.source, row.path)
    return done
