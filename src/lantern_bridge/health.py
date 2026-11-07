# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""Is the bridge in good shape: the sources answer, the scans are recent, there is room on the disk.

Each check is `ok`, `warn` (worth a look, the bridge still serves) or `fail` (it cannot do its job). The
whole is the worst of them. `/healthz` gives only the whole (a load balancer or a container needs no more);
the overview page lists the checks.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lantern_bridge.runtime import Runtime

OK, WARN, FAIL = "ok", "warn", "fail"
_ORDER = {OK: 0, WARN: 1, FAIL: 2}
LOW_DISK = 500 << 20  # below this, a warning (the default: the settings say `low_disk_mb`)
EMPTY_DISK = 20 << 20  # below this, the index cannot be written any more
STALE_SCAN = 1800.0  # seconds without a read of a source (a shop is read every 5 minutes)
OLD_ATLAS = 21 * 86400.0


@dataclass(frozen=True, slots=True)
class Check:
    name: str  # "source:<id>", "scan:<id>", "disk", "index", "atlas"
    status: str
    detail: str = ""


def run(runtime: Runtime, now: float | None = None) -> list[Check]:
    now = time.time() if now is None else now
    checks: list[Check] = []
    for source in runtime.config.sources:
        seen = runtime.stats.sources.get(source.id)
        if seen is None or seen.reachable is None:
            checks.append(Check(f"source:{source.id}", WARN, "not read yet"))
        elif not seen.reachable:
            checks.append(Check(f"source:{source.id}", FAIL, seen.error or "unreachable"))
        else:
            checks.append(Check(f"source:{source.id}", OK))
            if seen.scanned is not None and now - seen.scanned > STALE_SCAN:
                checks.append(Check(f"scan:{source.id}", WARN, f"last read {int((now - seen.scanned) // 60)} min ago"))
    try:
        runtime.index.counts()
        checks.append(Check("index", OK))
    except sqlite3.Error as error:  # a broken database is the one thing to say loudly
        checks.append(Check("index", FAIL, str(error)))
    try:
        free = shutil.disk_usage(runtime.state_dir).free
        status = FAIL if free < EMPTY_DISK else WARN if free < runtime.config.low_disk_mb << 20 else OK
        checks.append(Check("disk", status, f"{free >> 20} MB free"))
    except OSError as error:
        checks.append(Check("disk", WARN, str(error)))
    atlas = runtime.atlas
    if atlas is not None:
        if atlas.error and atlas.fetched is None:
            checks.append(Check("atlas", WARN, atlas.error))
        elif atlas.fetched is not None and now - atlas.fetched > OLD_ATLAS:
            checks.append(Check("atlas", WARN, f"the copy is {int((now - atlas.fetched) // 86400)} days old"))
        else:
            checks.append(Check("atlas", OK))
    return checks


def overall(checks: list[Check]) -> str:
    return max((c.status for c in checks), key=lambda s: _ORDER[s], default=OK)
