# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""A short text to an address of the administrator's when something needs a person.

Only a few journal lines are worth a phone buzzing: a source that stopped answering (urgent), a source that is
back, a newer release. The call is a POST of plain text with an ntfy-style `Title`, `Priority` and `Tags`, which
ntfy, Gotify (through its ntfy-compatible route) and most home-grown receivers read. No path and no title of the
library is in it. A call that fails is logged and dropped; nothing is queued for later.
"""

from __future__ import annotations

import logging

import httpx

from lantern_bridge.history import Event

log = logging.getLogger("lantern_bridge.notify")

TIMEOUT = 5.0
#: kind -> (title, priority 1-5, tag)
KINDS = {
    "source_down": ("Source down", "high", "warning"),
    "source_up": ("Source back", "default", "white_check_mark"),
    "update": ("Newer version out", "low", "package"),
}


class Notifier:
    def __init__(self, url: str, name: str, client: httpx.Client | None = None) -> None:
        self.url, self.name = url, name
        self._client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
        self.error: str | None = None

    def text(self, event: Event) -> str:
        what = KINDS[event.kind][0]
        parts = [f"{self.name}: {what.lower()}"]
        if event.subject:
            parts.append(event.subject)
        if event.detail:
            parts.append(event.detail)
        return " - ".join(parts)

    def on_event(self, event: Event) -> None:
        if event.kind not in KINDS:
            return
        title, priority, tag = KINDS[event.kind]
        try:
            reply = self._client.post(
                self.url,
                content=self.text(event).encode(),
                headers={
                    "Title": title,
                    "Priority": priority,
                    "Tags": tag,
                    "Content-Type": "text/plain; charset=utf-8",
                },
            )
            reply.raise_for_status()
        except httpx.HTTPError as error:
            self.error = (
                type(error).__name__
                if not isinstance(error, httpx.HTTPStatusError)
                else str(error.response.status_code)
            )
            log.warning("the notification was not taken: %s", self.error)
            return
        self.error = None

    def close(self) -> None:
        self._client.close()
