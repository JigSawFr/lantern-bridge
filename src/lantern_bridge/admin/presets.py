# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The presets of an account: a set of scopes, and the rating cap a child's account carries.

A preset is only a shortcut for the scopes (and the cap) the pages set on an account; the account keeps no memory
of it, and the page names the preset whose scopes an account holds (``preset_of``), or ``custom``.
"""

from __future__ import annotations

from collections.abc import Mapping

from lantern.models import RatingCap
from lantern.ratings import cap_problem
from lantern.server.accounts import ordered_scopes

from lantern_bridge.admin.auth import rank

#: Preset name to scopes. ``custom`` is the scopes ticked one by one.
PRESETS: dict[str, tuple[str, ...]] = {
    "full": ("library", "saves", "cheats", "requests", "sync.read", "sync.write"),
    "child": ("library", "saves", "requests", "sync.read", "sync.write"),
    "guest": ("library",),
}
#: The age a child's account is capped at when the form leaves it empty.
CHILD_AGE = 12
#: Scopes a manager may give; an administrator may give these and the ones below.
BASIC_SCOPES = ("library", "saves", "cheats", "requests", "sync.read", "sync.write")
#: Scopes that reach further than playing: writing cheats through to a shop, and the administration API.
ADVANCED_SCOPES = ("cheats.write", "admin")


def ordered(scopes: tuple[str, ...]) -> tuple[str, ...]:
    """These scopes in the order of the handshake."""
    return ordered_scopes(scopes)


def grantable(role: str | None) -> tuple[str, ...]:
    """The scopes an operator of this role may give to an account."""
    return (*BASIC_SCOPES, *ADVANCED_SCOPES) if rank(role) >= rank("admin") else BASIC_SCOPES


def holds_more_than(scopes: tuple[str, ...], role: str | None) -> bool:
    """Whether an account holds a scope that an operator of this role may not give (so may not change)."""
    return any(s not in grantable(role) for s in scopes)


def preset_of(scopes: tuple[str, ...]) -> str:
    """The name of the preset whose scopes these are, else ``custom``."""
    held = set(scopes)
    return next((name for name, wanted in PRESETS.items() if held == set(wanted)), "custom")


def scopes_from(preset: str, form: Mapping[str, str], role: str | None) -> tuple[tuple[str, ...], str | None]:
    """The scopes a form asks for, and the problem to show (one of the fixed texts of the pages), else None."""
    if preset in PRESETS:
        return PRESETS[preset], None
    if preset != "custom":
        return (), "Choose a preset from the list."
    ticked = [s for s in grantable(role) if form.get(f"scope.{s}")]
    if not ticked:
        return (), "Tick at least one scope."
    return ordered_scopes(ticked), None


def cap_from(preset: str, age_text: str, hide_unrated: bool) -> tuple[RatingCap | None, str | None]:
    """The rating cap a form asks for: none when the age is empty (a child's account then takes ``CHILD_AGE``)."""
    text = age_text.strip()
    if not text and preset == "child":
        text = str(CHILD_AGE)
    if not text:
        return None, None
    if not text.isdigit():
        return None, "An age is a number from 0 to 99."
    cap = RatingCap(age=int(text), unrated="hide" if hide_unrated else None)
    return (None, "An age is a number from 0 to 99.") if cap_problem(cap) else (cap, None)
