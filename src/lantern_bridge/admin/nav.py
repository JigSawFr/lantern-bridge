"""The header's menu: what each role sees, in one place, so the template only draws it."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Entry:
    key: str
    label: str
    href: str


@dataclass(frozen=True)
class Menu:
    main: list[Entry]  # what everybody comes for
    manage: list[Entry]  # the settings of the bridge, for admins only

    def holds(self, key: str) -> bool:
        return any(entry.key == key for entry in self.manage)


def build(role: str | None, developer: bool, _: Callable[..., str]) -> Menu:
    """The menu for a role; `None` (the sign-in page) has none."""
    if role is None:
        return Menu([], [])
    main = [
        Entry("overview", _("Overview"), "/admin/"),
        Entry("library", _("Library"), "/admin/library"),
        Entry("activity", _("Activity"), "/admin/activity"),
    ]
    if role == "observer":
        return Menu(main, [])
    main.append(Entry("events", _("Journal"), "/admin/events"))
    manage = [
        Entry("pairings", _("Pairings"), "/admin/pairings"),
        Entry("devices", _("Devices"), "/admin/devices"),
        Entry("accounts", _("Accounts"), "/admin/accounts"),
        Entry("invitations", _("Invitations"), "/admin/invitations"),
        Entry("sources", _("Sources"), "/admin/sources"),
        Entry("tasks", _("Tasks"), "/admin/tasks"),
        Entry("config", _("Settings"), "/admin/config"),
        Entry("logs", _("Logs"), "/admin/logs"),
        Entry("diagnostic", _("Diagnostic"), "/admin/diagnostic"),
    ]
    if developer:
        manage.append(Entry("developer", _("Developer"), "/admin/developer"))
    return Menu(main, manage)
