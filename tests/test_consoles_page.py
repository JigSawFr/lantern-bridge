"""A console, as the pages show and manage it: its page, a name of its own, rights taken away, a suspension that
keeps it paired, and the key to compare at pairing."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from lantern import signatures as sig
from lantern.models import RatingCap

from test_admin import _admin, _client
from test_roles import _csrf, _team

KEY = sig.DeviceKey(bytes(range(32)))


def _accounts(client: TestClient) -> Any:
    return client.app.state.accounts  # type: ignore[attr-defined]


def _console(client: TestClient, scopes: tuple[str, ...] = ("library", "saves", "cheats")) -> tuple[str, Any]:
    accounts = _accounts(client)
    if accounts.user("lea") is None:
        accounts.create_user("lea", ["library", "saves", "cheats", "requests"])
    token, device = accounts.issue("lea", "Living room", list(scopes), key=KEY.public)
    return str(token), device


def _events(client: TestClient) -> list[str]:
    return [e.subject or "" for e in client.app.state.runtime.history.events() if e.kind == "device_changed"]  # type: ignore[attr-defined]


def test_a_console_has_a_page_with_what_the_bridge_knows_of_it(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    _, device = _console(client)
    listing = client.get("/admin/devices").text
    assert f'href="/admin/devices/{device.id}"' in listing and "Living room" in listing
    page = client.get(f"/admin/devices/{device.id}")
    assert page.status_code == 200
    for shown in ("Living room", "lea", "library, saves, cheats", sig.short_fingerprint(KEY.public)):
        assert shown in page.text, shown
    assert client.get("/admin/devices/d_0000000000000000").status_code == 404
    assert f"/admin/devices/{device.id}" in client.get("/admin/accounts/lea").text  # and from its account's page


def test_a_console_is_renamed(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    token, device = _console(client)
    path = f"/admin/devices/{device.id}/rename"
    done = client.post(path, data={"csrf": _csrf(client, f"/admin/devices/{device.id}"), "name": "  Kids   room  "})
    assert done.status_code == 303 and done.headers["location"] == f"/admin/devices/{device.id}?m=device_changed"
    renamed = _accounts(client).device_of(token)
    assert renamed.device == "Kids room" and renamed.id == device.id  # tidied, same token, same id
    for bad in ("", "   ", "x" * 65):
        assert client.post(path, data={"csrf": _csrf(client, "/admin/devices"), "name": bad}).status_code == 400
    assert _accounts(client).device_of(token).device == "Kids room"
    assert _events(client) == ["Kids room"]


def test_rights_are_taken_away_from_a_console_and_never_added(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    token, device = _console(client)
    path = f"/admin/devices/{device.id}/rights"
    csrf = _csrf(client, f"/admin/devices/{device.id}")
    done = client.post(
        path, data={"csrf": csrf, "scope.library": "on", "scope.saves": "on", "age": "10", "hide_unrated": "on"}
    )
    assert done.status_code == 303
    after = _accounts(client).device_of(token)
    assert after.scopes == ("library", "saves") and after.rating_cap == RatingCap(age=10, unrated="hide")
    assert _accounts(client).user("lea").scopes == (
        "library",
        "saves",
        "cheats",
        "requests",
    )  # its account is untouched
    # a scope the console never had is not given by ticking it
    client.post(path, data={"csrf": csrf, "scope.library": "on", "scope.requests": "on"})
    assert _accounts(client).device_of(token).scopes == ("library",)
    # nothing ticked, or a bad age: refused, nothing changes
    for fields in ({}, {"scope.library": "on", "age": "abc"}, {"scope.library": "on", "age": "100"}):
        assert client.post(path, data={"csrf": csrf, **fields}).status_code == 400, fields
    assert _accounts(client).device_of(token).scopes == ("library",)
    # the age limit in force is the stricter of the console's and its account's
    _accounts(client).update_user("lea", rating_cap=RatingCap(age=7))
    client.post(path, data={"csrf": csrf, "scope.library": "on", "age": "16"})
    assert "7" in client.get(f"/admin/devices/{device.id}").text.split("Age limit", 1)[1][:200]


def test_a_console_is_suspended_and_lifted_and_stays_paired(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    token, device = _console(client)
    csrf = _csrf(client, f"/admin/devices/{device.id}")
    path = f"/admin/devices/{device.id}/suspend"

    def state() -> str:
        # signed as the console is bound to its key
        headers = {"Authorization": f"Bearer {token}"}
        sig.sign_request(KEY, "GET", "http://testserver/api/lantern/v1", headers, None)
        return str(client.get("/api/lantern/v1", headers=headers).json()["account"]["state"])

    assert state() == "active"
    assert client.post(path, data={"csrf": csrf, "state": "suspended"}).status_code == 303
    assert _accounts(client).device_of(token).suspended is True and state() == "frozen"
    assert "suspended" in client.get("/admin/devices").text
    assert client.post(path, data={"csrf": csrf, "state": "active"}).status_code == 303
    assert _accounts(client).device_of(token).suspended is False and state() == "active"
    assert client.post(path, data={"csrf": csrf, "state": "weird"}).status_code == 400
    assert any("suspended" in e for e in _events(client)) and any("active" in e for e in _events(client))


def test_a_revoked_console_has_no_page_any_more(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    token, device = _console(client)
    revoked = client.post(f"/admin/devices/{device.id}/revoke", data={"csrf": _csrf(client, "/admin/devices")})
    assert revoked.status_code == 303 and _accounts(client).device_of(token) is None
    assert client.get(f"/admin/devices/{device.id}").status_code == 404
    for action in ("rename", "rights", "suspend"):
        assert (
            client.post(
                f"/admin/devices/{device.id}/{action}", data={"csrf": _csrf(client, "/admin/devices")}
            ).status_code
            == 404
        )


def test_the_roles_that_may_manage_a_console(tmp_path: Path) -> None:
    team = _team(tmp_path)
    _, device = _console(team["owner"])
    page = f"/admin/devices/{device.id}"
    assert team["observer"].get(page).status_code == 403
    assert team["manager"].get(page).status_code == 200
    renamed = team["manager"].post(
        f"{page}/rename", data={"csrf": _csrf(team["manager"], page), "name": "From the manager"}
    )
    assert renamed.status_code == 303
    assert (
        team["observer"]
        .post(f"{page}/suspend", data={"csrf": _csrf(team["observer"]), "state": "suspended"})
        .status_code
        == 403
    )


def test_a_pairing_shows_the_key_to_compare_with_the_console(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    started = client.post(
        "/api/lantern/v1/pairing", json={"schema": 1, "device": "Switch", "scopes": ["library"], "key": KEY.document()}
    )
    assert started.status_code == 200, started.text
    page = client.get("/admin/pairings").text
    assert sig.short_fingerprint(KEY.public) in page and re.search(
        r"[0-9A-F]{4} [0-9A-F]{4} [0-9A-F]{4} [0-9A-F]{4}", page
    )
