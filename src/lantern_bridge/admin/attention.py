# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""What needs the administrator's attention, most urgent first, each with the way to deal with it.

The overview shows this list above everything else. Each item is a text (English, translated by the page), the
level it deserves, where to go, and, when one gesture settles it, the action behind a button.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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


def collect(runtime: Runtime, waiting: int, restart: bool, paired: int | None = None) -> list[Item]:
    items: list[Item] = []
    if not runtime.config.sources:
        items.append(Item(WAIT, "No source configured yet.", href="/admin/start", link="Start the setup"))
    elif paired == 0 and not runtime.config.public and not waiting:
        items.append(Item(INFO, "No device is paired yet", href="/admin/start", link="Setup guide"))
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
                "The settings file changed but was not applied: the bridge keeps running with the settings it has.",
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


def guide(
    runtime: Runtime, state: dict[str, Any], paired: int, uid: str, *, saved: list[str], restart: bool
) -> list[dict[str, Any]]:
    """The four steps from an empty bridge to a console that reads the library, each ticked by what the bridge sees."""
    has_source = bool(saved)  # the settings file: a source just added is there, applied or not
    read = state["total"] > 0
    served = state["titles"] > 0
    open_access = runtime.config.public
    steps: list[dict[str, Any]] = [
        {
            "title": "Add a source",
            "text": "A folder of games on this machine, or a Tinfoil, Ownfoil or AeroFoil shop.",
            "detail": ", ".join(saved),
            "done": has_source,
        },
        {
            "title": "Let the bridge read the files",
            "text": "The source you added was not applied: see the note on the settings page."
            if restart
            else "It reads them in place and never changes them.",
            "detail": f"{state['hashed']} / {state['total']}" if read else "",
            "done": has_source and not restart and read and state["pending"] == 0,
            "scan": has_source and not restart,
        },
        {
            "title": "Check the library",
            "text": "The titles the bridge found, with the problems it noticed.",
            "detail": f"{state['apps']} / {state['titles']}" if served else "",
            "done": served,
            "href": "/admin/library" if served else "",
            "link": "Open the library",
        },
        {
            "title": "Pair a device",
            "text": "Open Lantern on the console, add this server, then accept its request here."
            if not open_access
            else "The access is public: the console only needs the address of this server.",
            "detail": uid,
            "done": paired > 0 or open_access,
            "href": "/admin/pairings",
            "link": "Pairings",
        },
    ]
    first = next((n for n, step in enumerate(steps) if not step["done"]), None)
    if first is not None:
        steps[first]["current"] = True
    return steps
