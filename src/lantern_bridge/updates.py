# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""Is there a newer release: one read of GitHub's latest release a day, if the settings allow it.

Off by default, because it is the one call the bridge makes about itself. It sends nothing but the request (the
address of the machine reaches GitHub, as with any download); it reads the tag name of the latest release and
compares it with the running version. It never downloads or installs anything.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

import httpx

from lantern_bridge import __version__

log = logging.getLogger("lantern_bridge.updates")

RELEASES = "https://api.github.com/repos/JigSawFr/lantern-bridge/releases/latest"
PAGE = "https://github.com/JigSawFr/lantern-bridge/releases/tag/"
REFRESH = 24 * 3600.0
RETRY = 3600.0
_VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)$")


def parse(tag: str) -> tuple[int, ...] | None:
    """`v1.2.3` or `1.2` as numbers; None for anything else (a pre-release tag is not offered)."""
    found = _VERSION.match(tag.strip())
    return tuple(int(n) for n in found.group(1).split(".")) if found else None


def newer(latest: str | None, current: str) -> bool:
    a, b = parse(latest or ""), parse(current)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


class Updates:
    def __init__(self, current: str = __version__, url: str = RELEASES, client: httpx.Client | None = None) -> None:
        self.current, self.url = current, url
        self._client = client or httpx.Client(
            timeout=15.0,
            follow_redirects=False,
            headers={"User-Agent": "lantern-bridge", "Accept": "application/vnd.github+json"},
        )
        self.latest: str | None = None
        self.checked: float | None = None
        self.error: str | None = None

    @property
    def available(self) -> bool:
        return newer(self.latest, self.current)

    @property
    def page(self) -> str | None:
        return PAGE + (self.latest or "") if self.latest and parse(self.latest) is not None else None

    def check(self) -> bool:
        """Read the latest release; True when it worked (no release at all is a success with nothing to offer)."""
        try:
            reply = self._client.get(self.url)
            if reply.status_code == 404:  # no release published yet
                self.latest, self.error = None, None
            else:
                reply.raise_for_status()
                tag: Any = reply.json().get("tag_name")
                self.latest, self.error = (tag if isinstance(tag, str) else None), None
        except (httpx.HTTPError, ValueError, AttributeError) as error:
            self.error = type(error).__name__
            log.warning("the latest release could not be read: %s", self.error)
            return False
        finally:
            self.checked = time.time()
        return True

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            stop.wait(REFRESH if self.check() else RETRY)

    def close(self) -> None:
        self._client.close()
