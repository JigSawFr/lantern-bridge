# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""What a console would meet asking for a title: its sheet, its apps, and the bytes behind each file.

The same library the protocol routes answer from is asked directly (no request is made, no device paired):
each app of the title must have a file the library can give, the file must have the size the app says, and
a file served from a folder must open and be readable at its start and its end. A file relayed from a shop
is not read here (that would be a download).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lantern_bridge.runtime import Runtime

OK, FAIL, NOTE = "ok", "fail", "note"
_EDGE = 64 * 1024


@dataclass(frozen=True, slots=True)
class Step:
    status: str
    what: str
    detail: str = ""


def check(runtime: Runtime, title_id: str) -> list[Step]:
    title_id = title_id.upper()
    steps: list[Step] = []
    known = runtime.browser.catalog().titles.get(title_id)
    if known is None:
        return [Step(FAIL, "The index knows the title", "no file of the library carries this title id")]
    steps.append(Step(OK, "The index knows the title", f"{known.name}: {len(known.files)} file(s)"))
    library = runtime.projection.library
    sheet = runtime.projection.title(title_id)
    steps.append(
        Step(OK, "The sheet is given", sheet.name)
        if sheet is not None and sheet.name
        else Step(FAIL, "The sheet is given", "the library has no sheet for this title")
    )
    apps = [a for a in library.apps() if a.title_id.upper() == title_id]
    if not apps:
        steps.append(Step(FAIL, "Apps are offered", "no file of the title is hashed and offered yet"))
    pending = known.pending
    if pending:
        steps.append(Step(NOTE, "Hashing", f"{pending} file(s) wait to be hashed and are not offered yet"))
    for app in apps:
        label = f"{app.type} v{app.version_code} ({app.app_id})"
        ref = library.file(app.file.rsplit("/", 1)[-1])
        if ref is None:
            steps.append(Step(FAIL, label, "the library cannot give the file"))
            continue
        if ref.size != app.size:
            steps.append(Step(FAIL, label, f"the file says {ref.size} bytes, the app {app.size}"))
            continue
        if ref.data is not None or not ref.path:
            steps.append(Step(NOTE, label, "served from memory or relayed from a shop: not read here"))
            continue
        steps.append(_read(label, ref.path, ref.size))
    return steps


def _read(label: str, path: str, size: int) -> Step:
    try:
        on_disk = os.stat(path).st_size
        if on_disk != size:
            return Step(FAIL, label, f"{on_disk} bytes on disk, {size} in the index: scan again")
        with open(path, "rb") as handle:
            if not handle.read(min(_EDGE, size)) and size:
                return Step(FAIL, label, "the start of the file reads empty")
            if size > _EDGE:
                handle.seek(size - _EDGE)
                if len(handle.read(_EDGE)) != _EDGE:
                    return Step(FAIL, label, "the end of the file is shorter than expected")
    except OSError as error:
        return Step(FAIL, label, f"cannot be read: {error.strerror or error}")
    return Step(OK, label, f"{size} bytes, readable at both ends")
