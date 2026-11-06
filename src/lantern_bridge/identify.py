# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""Identify a Switch package from its file name: ``Name [0100ABCD00010000][v65536].nsp``.

The title id and the version come from the name; a file that does not carry
them is not guessed at (reading the package itself comes later).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

_FORMATS = frozenset({"nsp", "nsz", "xci", "xcz"})
_ID = re.compile(r"\[([0-9A-Fa-f]{16})\]")
_VERSION = re.compile(r"\[v(\d{1,10})\]")
_TAGS = re.compile(r"\s*\[[^\]]*\]|\s*\([^)]*\)")


@dataclass(frozen=True, slots=True)
class Identity:
    app_id: str
    title_id: str
    type: str
    version_code: int
    name: str
    format: str

    @property
    def priority(self) -> int:
        """Bases first, then updates, then DLC (the order of the hashing queue)."""
        return {"base": 0, "update": 1}.get(self.type, 2)


def identify(path: str) -> Identity | None:
    """What the name says, or None when it does not say enough.

    The name is the last path segment; a shop address that carries its name after a ``#`` (the way
    Tinfoil indexes do) gives the part after it.
    """
    file = PurePosixPath(path.rsplit("#", 1)[-1])
    fmt = file.suffix.lstrip(".").lower()
    if fmt not in _FORMATS:
        return None
    stem = file.stem
    found = _ID.search(stem)
    if found is None:
        return None
    app_id = found.group(1).upper()
    low = int(app_id, 16) & 0xFFF
    if low == 0x000:
        kind, title_id = "base", app_id
    elif low == 0x800:
        kind, title_id = "update", app_id[:13] + "000"
    else:
        kind = "dlc"
        title_id = f"{(int(app_id, 16) & ~0xFFF) - 0x1000:016X}"
    version = _VERSION.search(stem)
    name = _TAGS.sub("", stem).strip() or app_id
    return Identity(
        app_id=app_id,
        title_id=title_id,
        type=kind,
        version_code=int(version.group(1)) if version else 0,
        name=name,
        format=fmt,
    )
