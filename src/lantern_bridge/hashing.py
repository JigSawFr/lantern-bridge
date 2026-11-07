# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""Local hashing: the packages of the folders, queued by what the names say. Reading and recording the hashes is
the SDK's (``lantern.server.file_index``)."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lantern.server.file_index import Entry, IndexStore, hash_pending, scan, sha256_file

from lantern_bridge.compress import WORK
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
        if WORK in entry.path:
            continue  # a conversion in progress: its half-written file is not a package yet
        identity = identify(entry.path)
        if identity is not None:
            yield Entry(entry.path, entry.size, entry.mtime_ns, identity.priority)
        elif ignored is not None and entry.path.lower().endswith(_PACKAGE_SUFFIXES) and len(ignored) < IGNORED_LIMIT:
            ignored.append((entry.path, entry.size))


#: The most files hashed at once when the machine decides: a spinning disk or a network share is slowed by more.
AUTO_MAX = 4


def workers(asked: int = 0, cores: int | None = None) -> int:
    """How many files are hashed at once: the number asked, or (0) half the cores, from one to `AUTO_MAX`."""
    if asked > 0:
        return asked
    count = cores if cores is not None else (os.cpu_count() or 1)
    return max(1, min(AUTO_MAX, count // 2))


def hash_many(
    store: IndexStore,
    roots: Mapping[str, Path],
    *,
    threads: int,
    limit: int = 100,
    on_hashed: Callable[[str, str], object] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """`hash_pending` with several files read at once (hashlib lets go of the interpreter while it hashes, so the
    threads run on as many cores). A file that changed while it was read stays pending, as there; `on_hashed` is
    called one at a time."""
    if threads <= 1:
        return hash_pending(store, roots, limit=limit, on_hashed=on_hashed, should_stop=should_stop)
    rows = store.pending(limit, tuple(roots))
    lock = threading.Lock()

    def one(row) -> bool:  # type: ignore[no-untyped-def]
        if should_stop is not None and should_stop():
            return False
        file = roots[row.source] / row.path
        try:
            before = file.stat()
            if (before.st_size, before.st_mtime_ns) != (row.size, row.mtime_ns):
                return False
            digest = sha256_file(file)
            after = file.stat()
        except OSError:
            return False
        if (after.st_size, after.st_mtime_ns) != (row.size, row.mtime_ns):
            return False
        if not store.set_hash(row, digest):
            return False
        if on_hashed is not None:
            with lock:
                on_hashed(row.source, row.path)
        return True

    with ThreadPoolExecutor(max_workers=threads, thread_name_prefix="lantern-bridge-hash") as pool:
        return sum(pool.map(one, rows))


def sha_instructions() -> str:
    """What the processor offers for SHA-256, as far as the system says (Linux): the dedicated instructions or the
    vector ones OpenSSL falls back on; "unknown" where it cannot be read. For the diagnostic page, not a decision."""
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unknown"
    flags: set[str] = set()
    for line in text.splitlines():
        if line.startswith(("flags", "Features")):
            flags.update(line.partition(":")[2].split())
            break
    if flags & {"sha_ni", "sha2"}:
        return "dedicated instructions"
    if "avx2" in flags:
        return "vector instructions (AVX2)"
    return "plain instructions" if flags else "unknown"
