"""The accounts of the players, managed from the pages: made with a preset or scope by scope, an age limit,
changed, frozen, deleted, and chosen when a pairing is approved."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from lantern.models import RatingCap

from lantern_bridge.admin import presets
from test_admin import _admin, _client
from test_roles import _csrf, _team


def _accounts(client: TestClient) -> Any:
    return client.app.state.accounts  # type: ignore[attr-defined]


def _events(client: TestClient, kind: str) -> list[str]:
    return [e.subject or "" for e in client.app.state.runtime.history.events() if e.kind == kind]  # type: ignore[attr-defined]


def _make(client: TestClient, name: str, **fields: str) -> Any:
    return client.post("/admin/accounts", data={"csrf": _csrf(client, "/admin/accounts"), "name": name, **fields})


def test_an_account_is_made_with_a_preset(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    for name, preset in (("lea", "full"), ("sam", "child"), ("guest1", "guest")):
        reply = _make(client, name, preset=preset)
        assert reply.status_code == 303 and reply.headers["location"] == f"/admin/accounts/{name}?m=account_added"
        assert _accounts(client).user(name).scopes == presets.ordered(presets.PRESETS[preset])
    assert _accounts(client).user("lea").rating_cap is None
    assert _accounts(client).user("sam").rating_cap == RatingCap(age=presets.CHILD_AGE)  # a child's takes the usual
    assert sorted(_events(client, "account_added")) == ["guest1", "lea", "sam"]  # the journal is newest first
    page = client.get("/admin/accounts").text
    assert "sam" in page and "Child" in page and "Guest" in page and ">12<" in page


def test_an_account_is_made_scope_by_scope_with_an_age_limit(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    reply = _make(
        client, "ines", preset="custom", **{"scope.library": "on", "scope.saves": "on"}, age="7", hide_unrated="on"
    )
    assert reply.status_code == 303
    made = _accounts(client).user("ines")
    assert made.scopes == ("library", "saves")
    assert made.rating_cap == RatingCap(age=7, unrated="hide")
    assert "Custom" in client.get("/admin/accounts").text


def test_what_a_form_gets_wrong_is_refused_and_changes_nothing(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    _make(client, "lea", preset="full")
    for fields in (
        {"name": "lea", "preset": "full"},  # taken
        {"name": "bad name!", "preset": "full"},
        {"name": "", "preset": "full"},
        {"name": "x", "preset": "emperor"},
        {"name": "x", "preset": "custom"},  # no scope ticked
        {"name": "x", "preset": "full", "age": "abc"},
        {"name": "x", "preset": "full", "age": "120"},
        {"name": "x", "preset": "full", "age": "-3"},
    ):
        assert (
            client.post("/admin/accounts", data={"csrf": _csrf(client, "/admin/accounts"), **fields}).status_code == 400
        ), fields
    assert [u.name for u in _accounts(client).users()] == ["lea"]


def test_the_rights_of_an_account_are_changed_and_its_devices_follow(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    _make(client, "lea", preset="full")
    accounts = _accounts(client)
    token, device = accounts.issue("lea", "console", ["library", "saves", "cheats"])
    page = client.get("/admin/accounts/lea")
    assert page.status_code == 200 and "console" in page.text
    reply = client.post(
        "/admin/accounts/lea/edit", data={"csrf": _csrf(client, "/admin/accounts/lea"), "preset": "guest", "age": "10"}
    )
    assert reply.status_code == 303
    assert accounts.user("lea").scopes == ("library",) and accounts.user("lea").rating_cap == RatingCap(age=10)
    assert accounts.device_of(token).scopes == ("library",) and accounts.device_of(token).id == device.id
    again = client.post(
        "/admin/accounts/lea/edit", data={"csrf": _csrf(client, "/admin/accounts/lea"), "preset": "full"}
    )
    assert again.status_code == 303 and accounts.user("lea").rating_cap is None  # the age limit is gone with its field
    assert _events(client, "account_changed") == ["lea", "lea"]
    nothing = client.post(
        "/admin/accounts/lea/edit", data={"csrf": _csrf(client, "/admin/accounts/lea"), "preset": "custom"}
    )
    assert nothing.status_code == 400 and accounts.user("lea").scopes == presets.ordered(presets.PRESETS["full"])


def test_an_account_is_frozen_and_thawed(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    _make(client, "lea", preset="full")
    token = _csrf(client, "/admin/accounts/lea")
    assert client.post("/admin/accounts/lea/state", data={"csrf": token, "state": "frozen"}).status_code == 303
    assert _accounts(client).user("lea").state == "frozen" and "frozen" in client.get("/admin/accounts").text
    assert client.post("/admin/accounts/lea/state", data={"csrf": token, "state": "active"}).status_code == 303
    assert _accounts(client).user("lea").state == "active"
    assert client.post("/admin/accounts/lea/state", data={"csrf": token, "state": "weird"}).status_code == 400
    assert client.post("/admin/accounts/nobody/state", data={"csrf": token, "state": "frozen"}).status_code == 404


def test_deleting_an_account_takes_what_is_kept_for_it(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    _make(client, "lea", preset="full")
    _make(client, "sam", preset="full")
    accounts = _accounts(client)
    lea_token, _ = accounts.issue("lea", "console", ["library"])
    sam_token, _ = accounts.issue("sam", "console", ["library"])
    inv = accounts.create_invitation("lea", ["library"], "2999-01-01T00:00:00Z", 1)
    reply = client.post("/admin/accounts/lea/delete", data={"csrf": _csrf(client, "/admin/accounts/lea")})
    assert reply.status_code == 303 and reply.headers["location"] == "/admin/accounts?m=account_deleted"
    assert accounts.user("lea") is None and accounts.device_of(lea_token) is None and accounts.redeem(inv.code) is None
    assert accounts.user("sam") is not None and accounts.device_of(sam_token) is not None
    assert _events(client, "account_deleted") == ["lea"]
    assert client.get("/admin/accounts/lea").status_code == 404
    assert client.post("/admin/accounts/lea/delete", data={"csrf": _csrf(client, "/admin/accounts")}).status_code == 404


def test_a_manager_cannot_change_what_it_may_not_give(tmp_path: Path) -> None:
    team = _team(tmp_path)
    owner, manager, admin = team["owner"], team["manager"], team["admin"]
    _make(owner, "boss-kid", preset="full")
    _accounts(owner).create_user("service", ["library", "admin"])  # an account that reaches the administration API
    # a manager does not tick what is above it, and has no such box
    page = manager.get("/admin/accounts").text
    assert 'name="scope.library"' in page and 'name="scope.admin"' not in page
    custom = _make(manager, "x", preset="custom", **{"scope.admin": "on"})
    assert custom.status_code == 400 and _accounts(owner).user("x") is None
    # it does not edit or delete an account that holds such a scope, but may freeze it
    token = _csrf(manager, "/admin/accounts")
    assert manager.post("/admin/accounts/service/edit", data={"csrf": token, "preset": "guest"}).status_code == 403
    assert manager.post("/admin/accounts/service/delete", data={"csrf": token}).status_code == 403
    assert _accounts(owner).user("service").scopes == ("library", "admin")
    assert manager.post("/admin/accounts/service/state", data={"csrf": token, "state": "frozen"}).status_code == 303
    # an administrator does
    assert 'name="scope.admin"' in admin.get("/admin/accounts").text
    ok = admin.post("/admin/accounts/service/edit", data={"csrf": _csrf(admin, "/admin/accounts"), "preset": "guest"})
    assert ok.status_code == 303 and _accounts(owner).user("service").scopes == ("library",)


def _start_pairing(client: TestClient, device: str) -> str:
    started = client.post(
        "/api/lantern/v1/pairing", json={"schema": 1, "device": device, "scopes": ["library", "saves"]}
    )
    assert started.status_code in (200, 201, 202), started.text
    found = re.search(r'action="/admin/pairings/(pr_[^"]+)"', client.get("/admin/pairings").text)
    assert found
    return found.group(1)


def test_a_pairing_makes_the_account_with_the_preset_chosen(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    pairing = _start_pairing(client, "Kitchen Switch")
    token = _csrf(client, "/admin/pairings")
    bad = client.post(
        f"/admin/pairings/{pairing}", data={"csrf": token, "decision": "approve", "user": "sam", "preset": "emperor"}
    )
    assert bad.status_code == 400 and _accounts(client).user("sam") is None
    done = client.post(
        f"/admin/pairings/{pairing}", data={"csrf": token, "decision": "approve", "user": "sam", "preset": "child"}
    )
    assert done.status_code == 303
    sam = _accounts(client).user("sam")
    assert sam.scopes == presets.ordered(presets.PRESETS["child"]) and sam.rating_cap == RatingCap(
        age=presets.CHILD_AGE
    )
    assert [d.scopes for d in _accounts(client).devices("sam")] == [("library", "saves")]
    # an account that exists keeps its own rights, whatever preset the form carries
    second = _start_pairing(client, "Bedroom Switch")
    client.post(
        f"/admin/pairings/{second}",
        data={"csrf": _csrf(client, "/admin/pairings"), "decision": "approve", "user": "sam", "preset": "full"},
    )
    assert _accounts(client).user("sam").scopes == presets.ordered(presets.PRESETS["child"])


def test_an_invitation_makes_the_account_with_the_preset_chosen(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    made = client.post(
        "/admin/invitations",
        data={
            "csrf": _csrf(client, "/admin/invitations"),
            "user": "visitor",
            "preset": "guest",
            "devices": "1",
            "days": "1",
        },
    )
    assert made.status_code == 200
    assert _accounts(client).user("visitor").scopes == ("library",)
    refused = client.post(
        "/admin/invitations",
        data={
            "csrf": _csrf(client, "/admin/invitations"),
            "user": "other",
            "preset": "emperor",
            "devices": "1",
            "days": "1",
        },
    )
    assert refused.status_code == 400 and _accounts(client).user("other") is None


def test_the_presets_are_what_the_page_says() -> None:
    assert presets.preset_of(presets.ordered(presets.PRESETS["full"])) == "full"
    assert presets.preset_of(("library",)) == "guest" and presets.preset_of(("saves",)) == "custom"
    assert "cheats" not in presets.PRESETS["child"] and "cheats" in presets.PRESETS["full"]
    assert "admin" not in presets.grantable("manager") and "admin" in presets.grantable("admin")
    assert presets.cap_from("child", "", False) == (RatingCap(age=12), None)
    assert presets.cap_from("full", "", False) == (None, None)
    assert presets.cap_from("full", "7", True) == (RatingCap(age=7, unrated="hide"), None)
    assert presets.cap_from("full", "100", False)[1] is not None
