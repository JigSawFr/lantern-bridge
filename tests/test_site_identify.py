# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The website's "Will my files be recognised?" runs the bridge's naming rule in the page (site/assets/identify.js):
the same names go through it and through identify.py, and the two must agree."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lantern_bridge.identify import identify

SCRIPT = Path(__file__).resolve().parent.parent / "site" / "assets" / "identify.js"
NAMES = [
    "Frostline Tales [0100ABCD00010000][v0].nsp",
    "Frostline Tales [0100ABCD00010800][v196608].nsz",
    "Moss & Ember - Night Garden [0100abcd00021001][v0].xci",
    "Cinder Harbor (2025).nsp",
    "Paper Comet [0100ABCD00040000].zip",
    "shelf/sub/Tidepool Racers [0100ABCD00050000][v65536].xcz",
    "https://shop.example.org/files/12#Wind Garden [0100ABCD00060800][v1].NSP",
    "[0100ABCD00070000].nsp",
    "Odd [0100ABCD00080000][v12345678901].nsp",
    "Dotless [0100ABCD00090000]",
    "trailing dot [0100ABCD000A0000].",
    "Dir/",
]

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def test_the_page_reads_names_as_the_bridge_does() -> None:
    program = (
        f"const {{ identify }} = require({json.dumps(str(SCRIPT))});"
        f"console.log(JSON.stringify({json.dumps(NAMES)}.map((n) => identify(n))));"
    )
    seen = json.loads(subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True).stdout)
    for name, page in zip(NAMES, seen, strict=True):
        who = identify(name)
        if who is None:
            assert "why" in page, name
        else:
            assert page == {
                "app_id": who.app_id,
                "title_id": who.title_id,
                "type": who.type,
                "version_code": who.version_code,
                "name": who.name,
                "format": who.format,
            }, name
