# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""A change of settings is applied with no restart: the server swaps itself for a new one."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from fastapi.testclient import TestClient
from lantern.uid import new_uid

from lantern_bridge import reload as reload_module
from lantern_bridge.app import make_app
from lantern_bridge.config import Config, FolderSource, load, save
from test_admin import _admin, _csrf


def _app(tmp_path: Path, *, run: bool = False) -> tuple[TestClient, Path]:
    games = tmp_path / "games"
    games.mkdir(parents=True, exist_ok=True)
    config = Config(
        uid=new_uid(), name="First name", atlas=False, update_check=False, sources=[FolderSource("games", games)]
    )
    path = tmp_path / "state" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    save(path, config)
    app = make_app(config, tmp_path / "state", run=run, config_path=path)
    return TestClient(app, client=("127.0.0.1", 50000), follow_redirects=False), path


def _rename(path: Path, name: str) -> None:
    config = load(path)
    config.name = name
    save(path, config)


def test_a_save_from_the_pages_is_applied_with_no_restart(tmp_path: Path) -> None:
    client, _ = _app(tmp_path)
    admin = _admin(client)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    page = admin.get("/admin/config")
    csrf = _csrf(page.text)
    assert "First name" in page.text and "was not applied" not in page.text
    reply = admin.post("/admin/config/settings", data={"csrf": csrf, "name": "Second name", "rate": "0"})
    assert reply.status_code == 303, reply.text
    assert client.app.state.runtime is not runtime  # type: ignore[attr-defined]  # a new server runs
    page = admin.get("/admin/config")
    assert page.status_code == 200, "the administrator is still signed in"
    assert "Second name" in page.text and "was not applied" not in page.text
    assert client.app.state.runtime.config.name == "Second name"  # type: ignore[attr-defined]


def test_a_hand_edit_of_the_file_is_applied_when_the_server_looks(tmp_path: Path) -> None:
    client, path = _app(tmp_path)
    shell = client.app
    _rename(path, "Edited by hand")
    assert shell.state.runtime.config.name == "First name"  # type: ignore[attr-defined]
    assert asyncio.run(shell.reload()) is None  # type: ignore[attr-defined]
    assert shell.state.runtime.config.name == "Edited by hand"  # type: ignore[attr-defined]
    assert asyncio.run(shell.reload()) is None  # type: ignore[attr-defined]  # nothing changed: nothing swapped


def test_a_file_that_does_not_load_leaves_the_server_as_it_is_and_says_why(tmp_path: Path) -> None:
    client, path = _app(tmp_path)
    admin = _admin(client)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    path.write_text("this is [not toml")
    problem = asyncio.run(client.app.reload())  # type: ignore[attr-defined]
    assert problem and client.app.state.runtime is runtime  # type: ignore[attr-defined]
    page = admin.get("/admin/config").text
    assert "was not applied" in page and "toml" in page.lower()
    _rename_back = Config(uid=runtime.config.uid, name="Mended", sources=runtime.config.sources)
    path.unlink()  # `save` keeps the keys of the file it replaces, and this one is broken
    save(path, _rename_back)
    assert asyncio.run(client.app.reload()) is None  # type: ignore[attr-defined]
    assert "was not applied" not in admin.get("/admin/config").text


def test_a_server_that_cannot_be_built_keeps_the_old_one(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client, path = _app(tmp_path)
    shell = client.app
    runtime = shell.state.runtime  # type: ignore[attr-defined]
    real = shell._build  # type: ignore[attr-defined]

    def broken(config: Config, again: object) -> object:
        if config.name == "Broken":
            raise RuntimeError("no luck")
        return real(config, again)

    monkeypatch.setattr(shell, "_build", broken)
    _rename(path, "Broken")
    problem = asyncio.run(shell.reload())  # type: ignore[attr-defined]
    assert problem and "no luck" in problem and shell.state.runtime is runtime  # type: ignore[attr-defined]


def test_the_old_server_finishes_its_request_before_it_closes(tmp_path: Path) -> None:
    client, path = _app(tmp_path)
    shell = client.app
    old = shell.current  # type: ignore[attr-defined]
    closed: list[bool] = []
    original = shell._close_stores  # type: ignore[attr-defined]
    shell._close_stores = lambda app: (closed.append(True), original(app))  # type: ignore[attr-defined,func-returns-value]

    async def run() -> None:
        shell._busy[id(old)] = 1  # type: ignore[attr-defined]  # a download still going
        _rename(path, "Swapped")
        assert await shell.reload() is None  # type: ignore[attr-defined]
        await asyncio.sleep(0.3)
        assert not closed, "the old server is kept while it answers"
        shell._busy[id(old)] = 0  # type: ignore[attr-defined]
        await asyncio.sleep(0.5)
        assert closed, "and closed once it is done"

    asyncio.run(run())


def test_the_watcher_applies_a_hand_edit_by_itself(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(reload_module, "WATCH", 0.1)
    client, path = _app(tmp_path, run=True)
    with client:  # the lifespan starts the runtime and the watcher
        _rename(path, "Seen by the watcher")
        for _ in range(50):
            if client.app.state.runtime.config.name == "Seen by the watcher":  # type: ignore[attr-defined]
                break
            time.sleep(0.1)
        assert client.app.state.runtime.config.name == "Seen by the watcher"  # type: ignore[attr-defined]
        assert re.search(r"lantern-bridge-index", " ".join(t.name for t in __import__("threading").enumerate()))
