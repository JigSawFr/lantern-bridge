# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""What each role may open: the one table the gate, the menu and the pages' buttons all read.

A role goes as far as the roles before it (``auth.ROLES``), so a rule names the lowest role that may. A page or
a gesture no rule names needs an admin: a route added without thinking about this is closed to the observers and
the managers, not open to them.
"""

from __future__ import annotations

import re

from lantern_bridge.admin.auth import rank

#: (path, the lowest role, whether the rule is for reading only). The first rule that fits wins.
_RULES: tuple[tuple[re.Pattern[str], str, bool], ...] = tuple(
    (re.compile(pattern), role, reads)
    for pattern, role, reads in (
        # Everybody: signing out and one's own password.
        (r"/admin/(logout|me)", "observer", False),
        # The owner's: the operators, and what takes the whole bridge out or puts one in.
        (r"/admin/operators(/.*)?", "owner", False),
        (r"/admin/config/(export|import|backup)", "owner", False),
        # An admin's: tying an account to a user of an AeroFoil, which holds the name of a secret.
        (r"/admin/accounts/.+", "admin", False),
        # A manager's: who may use the bridge, and the consoles that do.
        (r"/admin/(accounts|pairings|devices|invitations|events)(/.*)?", "manager", False),
        # What an observer looks at, and cannot change.
        (r"/admin(/api/state|/api/badges|/activity|/live)?", "observer", True),
        (r"/admin/(library|static)(/.*)?", "observer", True),
    )
)

_READS = ("GET", "HEAD")


def minimum(method: str, path: str) -> str:
    """The lowest role that may make this request."""
    path = re.split(r"[?#]", path, maxsplit=1)[0].rstrip("/") or "/admin"
    for pattern, role, reads in _RULES:
        if pattern.fullmatch(path) and (not reads or method.upper() in _READS):
            return role
    return "admin"


def may(role: str | None, method: str, path: str) -> bool:
    """Whether an operator of `role` may make this request."""
    return rank(role) >= rank(minimum(method, path))
