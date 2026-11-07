"""Is there a newer release: off by default, one read a day, and nothing downloaded."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from lantern_bridge.config import Config, load, save
from lantern_bridge.mqtt import state
from lantern_bridge.updates import PAGE, Updates, newer, parse
from test_admin import _admin, _client


def _updates(status: int = 200, body: object = None, current: str = "0.1.0") -> tuple[Updates, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body if body is not None else {"tag_name": "v0.2.0"})

    return Updates(current, client=httpx.Client(transport=httpx.MockTransport(answer))), seen


@pytest.mark.parametrize(
    ("latest", "current", "expected"),
    [
        ("v0.2.0", "0.1.0", True),
        ("0.1.1", "0.1.0", True),
        ("v1.0", "0.9.9", True),
        ("0.1.0", "0.1.0", False),
        ("v0.1.0", "0.1.1", False),
        ("1.0", "1.0.0", False),  # the same version, written shorter
        ("v0.2.0-rc1", "0.1.0", False),  # a pre-release is not offered
        ("nightly", "0.1.0", False),
        (None, "0.1.0", False),
        ("v0.2.0", "dev", False),
    ],
)
def test_versions_are_compared_as_numbers(latest: str | None, current: str, expected: bool) -> None:
    assert newer(latest, current) is expected
    assert parse("v1.2.3") == (1, 2, 3) and parse("x") is None


def test_a_newer_release_is_found_and_linked() -> None:
    updates, seen = _updates()
    assert updates.check() and updates.latest == "v0.2.0" and updates.available and updates.error is None
    assert updates.page == PAGE + "v0.2.0" and seen[0].method == "GET" and "authorization" not in seen[0].headers
    own = Updates()  # the client it makes for itself says who asks, follows no redirect and sends no credential
    assert own._client.headers["user-agent"] == "lantern-bridge" and not own._client.follow_redirects
    assert "authorization" not in own._client.headers
    own.close()


def test_no_release_yet_is_not_an_error() -> None:
    updates, _ = _updates(status=404, body={"message": "Not Found"})
    assert updates.check() and updates.latest is None and not updates.available and updates.error is None
    assert updates.page is None and updates.checked is not None


@pytest.mark.parametrize("status", [403, 500])
def test_a_failed_read_says_why_and_keeps_the_last_answer(status: int) -> None:
    updates, _ = _updates()
    updates.check()
    updates._client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status)))
    assert not updates.check() and updates.error == "HTTPStatusError" and updates.latest == "v0.2.0"


def test_an_answer_that_is_not_a_release_is_ignored() -> None:
    updates, _ = _updates(body=["not", "an", "object"])
    assert not updates.check() and updates.latest is None
    other, _ = _updates(body={"tag_name": 5})
    assert other.check() and other.latest is None and not other.available


def test_the_setting_is_off_by_default_and_kept_in_the_file(tmp_path: Path) -> None:
    assert Config(uid="0c9c2a8e-5d3a-4c7e-8b0a-6f1d2e3c4b5a").update_check is True
    path = tmp_path / "config.toml"
    save(path, Config(uid="0c9c2a8e-5d3a-4c7e-8b0a-6f1d2e3c4b5a", update_check=False))
    assert "update_check = false" in path.read_text() and load(path).update_check is False
    path.write_text('schema = 1\nuid = "0c9c2a8e-5d3a-4c7e-8b0a-6f1d2e3c4b5a"\n')  # a file from before the setting
    assert load(path).update_check is True


def test_on_by_default_but_nothing_is_read_until_the_loop_runs(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    assert runtime.updates is not None and runtime.updates.checked is None  # the tests never start the loop
    assert "newer version" not in client.get("/admin/").text and "update" not in state(runtime)


def test_off_nothing_is_read_and_nothing_is_said(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from lantern.uid import new_uid

    from lantern_bridge.app import make_app

    config = Config(uid=new_uid(), update_check=False)
    client = _admin(
        TestClient(make_app(config, tmp_path / "state", run=False), client=("127.0.0.1", 50000), follow_redirects=False)
    )
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    assert runtime.updates is None and "newer version" not in client.get("/admin/").text
    assert "update" not in state(runtime) and 'name="update_check"' in client.get("/admin/config").text


def test_on_the_overview_and_the_state_say_a_newer_version_is_out(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    runtime.updates, _ = _updates(current="0.1.0")
    assert "update" not in state(runtime)  # not checked yet
    runtime.updates.check()
    page = client.get("/admin/").text
    assert "A newer version is out" in page and f'href="{PAGE}v0.2.0"' in page and 'rel="noreferrer"' in page
    assert state(runtime)["update"] == {"available": True, "latest": "v0.2.0"}
    runtime.updates.current = "0.2.0"
    assert "A newer version is out" not in client.get("/admin/").text


def test_the_settings_page_has_the_switch(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    path = tmp_path / "state" / "config.toml"
    page = client.get("/admin/config")
    assert 'name="update_check"' in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
    form = {"csrf": csrf, "name": "Shelf", "remote_rate": "10", "admin_lan_only": "on"}
    assert 'name="update_check" checked' in page.text.replace("\n", " ") or " checked>" in page.text
    assert client.post("/admin/config/settings", data=form).status_code == 303  # the box left unticked
    assert load(path).update_check is False
    assert client.post("/admin/config/settings", data={**form, "update_check": "on"}).status_code == 303
    assert load(path).update_check is True
