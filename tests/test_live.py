"""The live stream: a block of a page, sent when it changes, as the session that asks and nothing else."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lantern_bridge.admin import _ask
from test_admin import _admin, _client


def test_only_the_followed_addresses_are_streamed_and_a_stranger_is_refused(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    assert client.get("/admin/live?u=/admin/config").status_code == 404
    assert client.get("/admin/live?u=/admin/logs/stream").status_code == 404
    assert client.get("/admin/live?u=//evil.example.org/").status_code == 404
    assert client.get("/admin/live").status_code == 404
    stranger = _client(tmp_path / "other", sources=True)
    assert stranger.get("/admin/live?u=/admin/api/state", follow_redirects=False).status_code in (303, 401)


def test_the_observer_may_follow_what_it_may_see(tmp_path: Path) -> None:
    from test_observer import _with_observer

    _, eye = _with_observer(tmp_path)
    assert eye.get("/admin/live?u=/admin/config").status_code == 404  # not a followed address at all


@pytest.mark.parametrize("target", ["/admin/api/state", "/admin/?fragment=todo", "/admin/activity?fragment=1"])
def test_a_followed_address_answers_as_the_session_that_asks(tmp_path: Path, target: str) -> None:
    import anyio

    client = _admin(_client(tmp_path, sources=True))
    cookies = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 50000),
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"cookie", cookies.encode())],
        "app": client.app,
    }

    class Fake:
        def __init__(self) -> None:
            self.scope = scope
            self.app = client.app

    async def go() -> tuple[str | None, str | None]:
        mine = await _ask(Fake(), target)  # type: ignore[arg-type]
        scope["headers"] = [(b"host", b"testserver")]
        return mine, await _ask(Fake(), target)  # type: ignore[arg-type]

    mine, anonymous = anyio.run(go)
    assert mine and anonymous is None  # without the cookie the page is a redirect, not a 200
    if target.endswith("state"):
        assert "hashed" in json.loads(mine)
    assert asyncio.iscoroutinefunction(_ask)
