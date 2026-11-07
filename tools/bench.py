# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""A load test on a big fake library: how long a first scan, the hashing, the catalog and a search take.

    uv run python tools/bench.py [--titles 50000] [--keep]

The library is made of tiny files named like real ones (a base game, most with an update, some with a DLC), in a
temporary folder that is removed afterwards. The files are tiny on purpose: the figures are those of the bridge
(index, projection, catalog), not of the disk. Hashing a real library is bound by how fast the volume reads.
"""

from __future__ import annotations

import argparse
import resource
import shutil
import sys
import tempfile
import time
from pathlib import Path

from lantern_bridge.catalog import search
from lantern_bridge.config import Config, FolderSource
from lantern_bridge.runtime import Runtime
from lantern_bridge.upstream import BridgeLibrary

UID = "0c9c2a8e-5d3a-4c7e-8b0a-6f1d2e3c4b5a"
WORDS = ("Aurora", "Harbor", "Kite", "Night", "Quest", "Race", "Garden", "Signal", "Lantern", "Orbit", "Maple", "Echo")


def make(root: Path, titles: int) -> int:
    root.mkdir(parents=True, exist_ok=True)
    files = 0
    for n in range(titles):
        base = f"01{n:08X}000000"[:13] + "000"  # a base title id ends with 000
        name = f"{WORDS[n % len(WORDS)]} {WORDS[(n // len(WORDS)) % len(WORDS)]} {n}"
        (root / f"{name} [{base}][v0].nsp").write_bytes(b"b%d" % n)
        files += 1
        if n % 5:  # four titles in five have an update
            (root / f"{name} [{base[:13]}800][v65536].nsp").write_bytes(b"u%d" % n)
            files += 1
        if n % 4 == 0:  # one in four has a DLC
            (root / f"{name} DLC [{int(base[:13], 16) + 1:013X}001][v0].nsp").write_bytes(b"d%d" % n)
            files += 1
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--titles", type=int, default=50_000)
    parser.add_argument("--batch", type=int, default=500, help="files hashed per pass")
    parser.add_argument("--keep", action="store_true", help="keep the temporary folder")
    args = parser.parse_args()
    work = Path(tempfile.mkdtemp(prefix="lantern-bench-"))
    try:
        began = time.perf_counter()
        files = make(work / "games", args.titles)
        print(f"made {files} files for {args.titles} titles in {time.perf_counter() - began:.1f}s")
        config = Config(uid=UID, name="Bench", sources=[FolderSource("games", work / "games")])
        runtime = Runtime(config, work / "state", BridgeLibrary(base="/api/lantern"))
        began = time.perf_counter()
        runtime.cycle(batch=args.batch)  # the first scan plus one batch of hashing
        print(f"first scan + {args.batch} hashed: {time.perf_counter() - began:.1f}s")
        began, passes = time.perf_counter(), 0
        while runtime.index.counts()["pending"]:
            runtime.cycle(batch=args.batch)
            passes += 1
        took = time.perf_counter() - began
        print(f"hashing the rest: {took:.1f}s in {passes} passes ({files / took:.0f} files/s, tiny files)")
        began = time.perf_counter()
        runtime.cycle(batch=args.batch)
        print(f"a quiet pass (nothing changed): {time.perf_counter() - began:.2f}s")
        began = time.perf_counter()
        catalog = runtime.browser.catalog()
        print(f"catalog built: {time.perf_counter() - began:.2f}s, {len(catalog.titles)} titles")
        for label, kw in (
            ("first page", {}),
            ("search 'harbor'", {"q": "harbor"}),
            ("search by id", {"q": "0100000A"}),
            ("filter DLC", {"kind": "dlc"}),
            ("sort by version", {"sort": "version"}),
            ("page 500", {"page": 500}),
        ):
            began = time.perf_counter()
            page = search(catalog, **kw)  # type: ignore[arg-type]
            print(f"{label}: {(time.perf_counter() - began) * 1000:.0f} ms ({page.total} results)")
        state = work / "state"
        sizes = ", ".join(f"{p.name} {p.stat().st_size / 1e6:.1f} MB" for p in sorted(state.glob("*.sqlite3")))
        print(f"on disk: {sizes}")
        print(f"peak memory: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MiB")
        runtime.stop()
    finally:
        if args.keep:
            print(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
