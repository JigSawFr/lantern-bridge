# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""A bridge over a made-up library, for the screenshots of the website (tools/site_shots.mjs).

python tools/demo_serve.py PORT   (prints "ready" once the library is hashed)

The games are made up (names and ids), the files are sparse and empty; nothing here is a real library.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import uvicorn
from lantern import signatures as sig
from lantern.uid import new_uid

from lantern_bridge.admin import presets
from lantern_bridge.app import make_app
from lantern_bridge.config import Config, FolderSource

PASSWORD = "correct horse battery staple"
MB = 1024 * 1024
# name, title number, size in MB, version code of the update (0: none), size of the update in MB
GAMES = [
    ("Frostline Tales", 1, 560, 196608, 90),
    ("Moss & Ember", 2, 300, 65536, 40),
    ("Cinder Harbor", 3, 420, 0, 0),
    ("Paper Comet", 4, 180, 65536, 25),
    ("Tidepool Racers", 5, 640, 131072, 60),
    ("Lumen Drift", 6, 240, 0, 0),
    ("Orchard Knights", 7, 520, 65536, 70),
    ("Saltmarsh Detective", 8, 150, 0, 0),
    ("Kite Valley", 9, 380, 131072, 35),
    ("Ember Quarry", 10, 270, 0, 0),
]


def build(root: Path):  # type: ignore[no-untyped-def]
    games = root / "games"
    games.mkdir(parents=True)
    for name, number, size, update, update_size in GAMES:
        title = f"0100ABCD{number:04X}0000"
        with (games / f"{name} [{title}][v0].nsp").open("wb") as file:
            file.truncate(size * MB)
        if update:
            with (games / f"{name} [{title[:13]}800][v{update}].nsp").open("wb") as file:
                file.truncate(update_size * MB)
    config = Config(uid=new_uid(), name="Living room", atlas=False, sources=[FolderSource("games", games)])
    app = make_app(config, root / "state", run=False)
    store = app.state.admin
    assert store.create(store.setup_code() or "", "maya", PASSWORD) is None
    while app.state.runtime.cycle(batch=50):
        pass
    people(app)
    return app


def people(app) -> None:  # type: ignore[no-untyped-def]
    """Made-up operators, accounts and consoles, for the accounts pages (first names that belong to no one here)."""
    store = app.state.admin
    assert store.add("theo", "manager", PASSWORD) is None
    assert store.add("ines", "observer", PASSWORD) is None
    accounts = app.state.accounts
    accounts.create_user("noah", presets.PRESETS["full"])
    accounts.create_user("mia", presets.PRESETS["child"])
    accounts.update_user("mia", rating_cap=presets.cap_from("child", "10", False)[0])
    accounts.create_user("zoe", presets.PRESETS["guest"])
    accounts.create_user("sam", presets.PRESETS["full"], state="frozen")
    for number, (user, device) in enumerate(
        (("noah", "Living room"), ("noah", "Travel Lite"), ("mia", "Kids room"), ("zoe", "Guest console"))
    ):
        key = sig.DeviceKey(bytes([number + 1] * 32)).public
        accounts.issue(user, device, accounts.user(user).scopes, key=key)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        application = build(Path(directory))
        print("ready", flush=True)
        uvicorn.run(application, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
