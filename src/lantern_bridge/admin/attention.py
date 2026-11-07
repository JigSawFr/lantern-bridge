# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""What needs the administrator's attention, most urgent first, each with the way to deal with it.

The overview shows this list above everything else. Each item is a text (English, translated by the page), the
level it deserves, where to go, and, when one gesture settles it, the action behind a button.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from lantern_bridge import health

if TYPE_CHECKING:
    from lantern_bridge.runtime import Runtime

BAD, WAIT, INFO = "bad", "wait", "info"
_ORDER = {BAD: 0, WAIT: 1, INFO: 2}


@dataclass(frozen=True, slots=True)
class Item:
    level: str  # bad (failing), wait (worth a look), info (for your information)
    text: str  # English, translated by the page
    detail: str = ""  # a name or a figure, shown as is
    href: str = ""  # where to deal with it
    link: str = ""  # the words of the link (English)
    scan: str = ""  # a source to read again: the page shows a button when set
    external: bool = False


def collect(runtime: Runtime, waiting: int, restart: bool) -> list[Item]:
    items: list[Item] = []
    if not runtime.config.sources:
        items.append(Item(WAIT, "No source configured yet.", href="/admin/config", link="Add one"))
    for check in health.run(runtime):
        if check.status == health.OK:
            continue
        level = BAD if check.status == health.FAIL else WAIT
        kind, _, name = check.name.partition(":")
        if kind == "source":
            items.append(Item(level, "A source does not answer", f"{name}: {check.detail}".strip(": "), scan=name))
        elif kind == "scan":
            items.append(Item(level, "A source has not been read for a while", f"{name}: {check.detail}", scan=name))
        elif kind == "disk":
            items.append(
                Item(level, "The disk of the bridge is nearly full", check.detail, "/admin/config", "Settings")
            )
        elif kind == "index":
            items.append(Item(level, "The index of hashes is broken", check.detail, "/admin/logs", "Logs"))
        elif kind == "atlas":
            items.append(Item(level, "Atlas data is missing or old", check.detail, "/admin/config", "Settings"))
        else:  # a check the page does not know yet is still shown
            items.append(Item(level, "A check needs a look", f"{check.name}: {check.detail}".strip(": ")))
    if restart:
        items.append(
            Item(
                WAIT,
                "The settings file changed: restart the bridge to apply it.",
                href="/admin/config",
                link="Settings",
            )
        )
    if waiting:
        items.append(Item(INFO, "Devices are waiting to be paired", str(waiting), "/admin/pairings", "Pairings"))
    updates = runtime.updates
    if updates is not None and updates.available:
        items.append(
            Item(
                INFO, "A newer version is out:", str(updates.latest), updates.page or "", "Release notes", external=True
            )
        )
    return sorted(items, key=lambda item: _ORDER[item.level])
