"""A bridge for the browser tests: fake titles, an administrator already made, no network (no Atlas).

python tests/browser/serve.py PORT   (prints "ready" once the library is indexed)
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import uvicorn
from lantern.uid import new_uid

from lantern_bridge.app import make_app
from lantern_bridge.config import Config, FolderSource

PASSWORD = "correct horse battery staple"
TITLES = {"Aurora Quest": "0100AAAA00000000", "Night Harbor": "0100BBBB00000000", "Kite Race": "0100CCCC00000000"}


def build(root: Path):  # type: ignore[no-untyped-def]
    games = root / "games"
    games.mkdir(parents=True)
    for name, title_id in TITLES.items():
        (games / f"{name} [{title_id}][v0].nsp").write_bytes(name.encode() * 64)
    config = Config(uid=new_uid(), name="Browser tests", atlas=False, sources=[FolderSource("games", games)])
    app = make_app(config, root / "state", run=False)
    store = app.state.admin
    assert store.create(store.setup_code() or "", "boss", PASSWORD) is None
    app.state.runtime.cycle(batch=50)
    return app


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        application = build(Path(directory))
        print("ready", flush=True)
        uvicorn.run(application, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
