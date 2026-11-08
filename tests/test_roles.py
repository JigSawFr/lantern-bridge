"""The operators of the pages and their four roles: what each may open, how the owner manages them, and the file
they live in."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lantern_bridge.__main__ import main
from lantern_bridge.admin.auth import ROLES, AdminSchemaError, AdminStore
from lantern_bridge.admin.permissions import may, minimum
from test_admin import PASSWORD, _admin, _client

PASSWORDS = {"observer": "watcher-pass-1", "manager": "manager-pass-12", "admin": "admin-pass-1234"}

#: What each page or gesture needs, written out by hand: the table must give exactly this.
EXPECTED = {
    ("GET", "/admin/"): "observer",
    ("GET", "/admin"): "observer",
    ("GET", "/admin/api/state"): "observer",
    ("GET", "/admin/activity"): "observer",
    ("GET", "/admin/library"): "observer",
    ("GET", "/admin/library/export"): "observer",
    ("GET", "/admin/static/app.css"): "observer",
    ("GET", "/admin/me"): "observer",
    ("POST", "/admin/me"): "observer",
    ("POST", "/admin/logout"): "observer",
    ("GET", "/admin/events"): "manager",
    ("GET", "/admin/events/export"): "manager",
    ("GET", "/admin/pairings"): "manager",
    ("POST", "/admin/pairings/x"): "manager",
    ("GET", "/admin/devices"): "manager",
    ("POST", "/admin/devices/x/revoke"): "manager",
    ("GET", "/admin/accounts"): "manager",
    ("GET", "/admin/invitations"): "manager",
    ("POST", "/admin/invitations"): "manager",
    ("POST", "/admin/invitations/x/revoke"): "manager",
    ("POST", "/admin/accounts/lea/login"): "admin",
    ("POST", "/admin/accounts/lea/login/remove"): "admin",
    ("POST", "/admin/library/rehash"): "admin",
    ("POST", "/admin/library/x/delete"): "admin",
    ("POST", "/admin/scan"): "admin",
    ("GET", "/admin/sources"): "admin",
    ("GET", "/admin/sessions"): "admin",
    ("GET", "/admin/config"): "admin",
    ("POST", "/admin/config/settings"): "admin",
    ("GET", "/admin/homebrew"): "admin",
    ("GET", "/admin/tasks"): "admin",
    ("GET", "/admin/logs"): "admin",
    ("GET", "/admin/diagnostic"): "admin",
    ("GET", "/admin/developer"): "admin",
    ("GET", "/admin/start"): "admin",
    ("GET", "/admin/operators"): "owner",
    ("POST", "/admin/operators"): "owner",
    ("POST", "/admin/operators/role"): "owner",
    ("POST", "/admin/operators/password"): "owner",
    ("POST", "/admin/operators/remove"): "owner",
    ("POST", "/admin/config/backup"): "owner",
    ("GET", "/admin/config/export"): "owner",
    ("POST", "/admin/config/import"): "owner",
}


def _csrf(client: TestClient, path: str = "/admin/me") -> str:
    found = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
    assert found
    return found.group(1)


def _team(tmp_path: Path) -> dict[str, TestClient]:
    """The owner, and one signed-in operator of each other role."""
    owner = _admin(_client(tmp_path, sources=True))
    team = {"owner": owner}
    for number, (role, password) in enumerate(PASSWORDS.items(), start=1):
        added = owner.post(
            "/admin/operators",
            data={"csrf": _csrf(owner), "name": f"{role}-op", "role": role, "password": password},
        )
        assert added.status_code == 303, role
        other = TestClient(owner.app, client=("127.0.0.1", 50000 + number), follow_redirects=False)
        assert other.post("/admin/login", data={"name": f"{role}-op", "password": password}).status_code == 303
        team[role] = other
    return team


def test_the_table_gives_each_page_the_role_written_out_here() -> None:
    for (method, path), role in EXPECTED.items():
        assert minimum(method, path) == role, (method, path)


def test_a_role_goes_as_far_as_the_ones_before_it() -> None:
    for (method, path), role in EXPECTED.items():
        for candidate in ROLES:
            assert may(candidate, method, path) == (ROLES.index(candidate) >= ROLES.index(role)), (candidate, path)
    assert not may(None, "GET", "/admin/") and not may("stranger", "GET", "/admin/")


def test_what_no_rule_names_is_for_the_admins_and_above() -> None:
    for path in ("/admin/something-new", "/admin/library/x/convert", "/admin/developer/anything"):
        assert minimum("POST", path) == "admin", path
    assert minimum("GET", "/admin/library/x") == "observer" and minimum("POST", "/admin/library/x/rehash") == "admin"
    assert minimum("GET", "/admin/sources#now") == "admin" and minimum("GET", "/admin/library?filter=x") == "observer"


def test_every_role_is_refused_what_is_above_it_and_only_that(tmp_path: Path) -> None:
    team = _team(tmp_path)
    for (method, path), needed in EXPECTED.items():
        if path.startswith("/admin/static") or path in ("/admin", "/admin/logout"):
            continue
        for role in ("observer", "manager", "admin"):
            client, csrf = team[role], _csrf(team[role])
            if method == "GET":
                reply = client.get(path)
            else:
                reply = client.post(path, data={"csrf": csrf})
            refused = reply.status_code == 403 and "Not for this account" in reply.text
            assert refused == (ROLES.index(role) < ROLES.index(needed)), (role, method, path, reply.status_code)


def test_each_role_sees_its_own_menu(tmp_path: Path) -> None:
    team = _team(tmp_path)

    def menu(role: str) -> str:
        html = team[role].get("/admin/").text
        return str(
            html.split("<nav", 1)[1].split("</nav>", 1)[0] + html.split('class="more"', 1)[-1].split("</div>", 1)[0]
        )

    assert 'href="/admin/pairings"' not in menu("observer") and 'href="/admin/events"' not in menu("observer")
    managers = menu("manager")
    for shown in ("pairings", "devices", "accounts", "invitations", "events"):
        assert f'href="/admin/{shown}"' in managers, shown
    for hidden in ("config", "sources", "operators", "logs", "diagnostic", "sessions"):
        assert f'href="/admin/{hidden}"' not in managers, hidden
    admins = menu("admin")
    assert 'href="/admin/config"' in admins and 'href="/admin/operators"' not in admins
    assert 'href="/admin/operators"' in menu("owner")


def test_an_admin_does_not_see_the_backup_nor_the_transfer_of_the_settings(tmp_path: Path) -> None:
    team = _team(tmp_path)
    assert "/admin/config/backup" not in team["admin"].get("/admin/config").text
    assert "/admin/config/backup" in team["owner"].get("/admin/config").text


def test_a_manager_decides_on_consoles_but_sees_no_secret_names(tmp_path: Path) -> None:
    team = _team(tmp_path)
    assert team["manager"].get("/admin/pairings").status_code == 200
    assert team["manager"].get("/admin/accounts").status_code == 200
    assert team["manager"].get("/admin/events").status_code == 200
    assert team["manager"].post("/admin/scan", data={"csrf": _csrf(team["manager"])}).status_code == 403


def test_the_command_box_offers_the_scan_only_to_those_who_may_ask_for_it(tmp_path: Path) -> None:
    team = _team(tmp_path)
    for role, offered in (("observer", False), ("manager", False), ("admin", True), ("owner", True)):
        assert ("data-csrf=" in team[role].get("/admin/").text) == offered, role


def test_the_owner_adds_an_operator_and_the_names_and_passwords_are_checked(tmp_path: Path) -> None:
    owner = _admin(_client(tmp_path))
    token = _csrf(owner)
    for data in (
        {"name": "boss", "role": "admin", "password": PASSWORDS["admin"]},
        {"name": "BOSS", "role": "admin", "password": PASSWORDS["admin"]},
        {"name": "w", "role": "admin", "password": "short"},
        {"name": "", "role": "admin", "password": PASSWORDS["admin"]},
        {"name": "w", "role": "root", "password": PASSWORDS["admin"]},
    ):
        assert owner.post("/admin/operators", data={"csrf": token, **data}).status_code == 400, data
    assert [o.name for o in AdminStore(tmp_path / "state" / "admin.json").operators()] == ["boss"]
    page = owner.get("/admin/operators")
    assert page.status_code == 200 and "boss" in page.text


def test_changing_a_role_takes_effect_in_the_sessions_already_open(tmp_path: Path) -> None:
    team = _team(tmp_path)
    assert team["manager"].get("/admin/sources").status_code == 403
    owner = team["owner"]
    done = owner.post("/admin/operators/role", data={"csrf": _csrf(owner), "name": "manager-op", "role": "admin"})
    assert done.status_code == 303
    assert team["manager"].get("/admin/sources").status_code == 200
    owner.post("/admin/operators/role", data={"csrf": _csrf(owner), "name": "manager-op", "role": "observer"})
    assert team["manager"].get("/admin/pairings").status_code == 403


def test_the_last_owner_keeps_the_role_and_nobody_removes_themselves(tmp_path: Path) -> None:
    owner = _admin(_client(tmp_path))
    token = _csrf(owner)
    assert owner.post("/admin/operators/role", data={"csrf": token, "name": "boss", "role": "admin"}).status_code == 400
    assert owner.post("/admin/operators/remove", data={"csrf": token, "name": "boss"}).status_code == 400
    owner.post(
        "/admin/operators", data={"csrf": token, "name": "second", "role": "owner", "password": "second-pass-12"}
    )
    assert owner.post("/admin/operators/role", data={"csrf": token, "name": "boss", "role": "admin"}).status_code == 303
    assert owner.get("/admin/operators").status_code == 403  # no longer an owner, at once
    assert AdminStore(tmp_path / "state" / "admin.json").identify("boss", PASSWORD) == "admin"


def test_removing_an_operator_closes_its_sessions_and_its_password_stops_working(tmp_path: Path) -> None:
    team = _team(tmp_path)
    owner = team["owner"]
    assert team["admin"].get("/admin/sources").status_code == 200
    removed = owner.post("/admin/operators/remove", data={"csrf": _csrf(owner), "name": "admin-op"})
    assert removed.status_code == 303
    assert team["admin"].get("/admin/sources").status_code == 303  # to the sign-in
    fresh = TestClient(owner.app, client=("127.0.0.1", 50077), follow_redirects=False)
    assert fresh.post("/admin/login", data={"name": "admin-op", "password": PASSWORDS["admin"]}).status_code == 401


def test_the_owner_sets_a_password_and_the_old_one_stops_working(tmp_path: Path) -> None:
    team = _team(tmp_path)
    owner = team["owner"]
    done = owner.post(
        "/admin/operators/password", data={"csrf": _csrf(owner), "name": "manager-op", "password": "brand-new-pass-1"}
    )
    assert done.status_code == 303
    assert team["manager"].get("/admin/pairings").status_code == 303  # its session ended
    store = AdminStore(tmp_path / "state" / "admin.json")
    assert store.identify("manager-op", "brand-new-pass-1") == "manager"
    assert store.identify("manager-op", PASSWORDS["manager"]) is None
    short = owner.post(
        "/admin/operators/password", data={"csrf": _csrf(owner), "name": "manager-op", "password": "short"}
    )
    assert short.status_code == 400


def test_an_operator_changes_its_own_password_and_keeps_only_this_session(tmp_path: Path) -> None:
    team = _team(tmp_path)
    mine = team["observer"]
    second = TestClient(mine.app, client=("127.0.0.1", 50090), follow_redirects=False)
    assert (
        second.post("/admin/login", data={"name": "observer-op", "password": PASSWORDS["observer"]}).status_code == 303
    )
    token = _csrf(mine)
    base = {"csrf": token, "current": PASSWORDS["observer"], "password": "another-pass-12", "again": "another-pass-12"}
    assert mine.post("/admin/me", data={**base, "current": "not-it-at-all"}).status_code == 400
    assert mine.post("/admin/me", data={**base, "again": "another-pass-13"}).status_code == 400
    assert mine.post("/admin/me", data={**base, "password": "short", "again": "short"}).status_code == 400
    assert mine.post("/admin/me", data=base).status_code == 303
    assert mine.get("/admin/").status_code == 200  # this one stays
    assert second.get("/admin/").status_code == 303  # the other ends
    store = AdminStore(tmp_path / "state" / "admin.json")
    assert store.identify("observer-op", "another-pass-12") == "observer"
    assert store.identify("observer-op", PASSWORDS["observer"]) is None


def test_guessing_the_current_password_is_slowed_down(tmp_path: Path) -> None:
    mine = _team(tmp_path)["manager"]
    token = _csrf(mine)
    data = {"csrf": token, "current": "wrong-one-1234", "password": "another-pass-12", "again": "another-pass-12"}
    codes = [mine.post("/admin/me", data=data).status_code for _ in range(12)]
    assert 429 in codes and codes[0] == 400


# The file the operators live in.


def _file(tmp_path: Path, data: dict[str, object]) -> Path:
    path = tmp_path / "admin.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_a_file_of_schema_1_is_taken_up_once(tmp_path: Path) -> None:
    # An administrator and an observer, as schema 1 wrote them
    from lantern_bridge.admin.auth import _SCRYPT, _hash

    def entry(name: str, password: str) -> dict[str, object]:
        salt = b"\x01" * 16
        return {"name": name, "salt": salt.hex(), "hash": _hash(password, salt, **_SCRYPT), "scrypt": _SCRYPT}

    path = _file(
        tmp_path,
        {"schema": 1, "future_key": "kept", **entry("boss", PASSWORD), "observer": entry("watcher", "watch-pass-123")},
    )
    store = AdminStore(path)
    assert [(o.name, o.role) for o in store.operators()] == [("boss", "owner"), ("watcher", "observer")]
    assert store.identify("boss", PASSWORD) == "owner" and store.identify("watcher", "watch-pass-123") == "observer"
    assert store.configured
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["schema"] == 2 and "observer" not in written and "hash" not in written  # the old shape is gone
    assert written["future_key"] == "kept"  # what this version does not know stays
    again = AdminStore(path)  # nothing left to take up
    assert [o.name for o in again.operators()] == ["boss", "watcher"]


def test_a_newer_file_is_not_read_nor_written(tmp_path: Path) -> None:
    path = _file(tmp_path, {"schema": 3, "operators": [{"name": "x", "role": "owner"}]})
    before = path.read_bytes()
    store = AdminStore(path)
    with pytest.raises(AdminSchemaError):
        store.configured  # noqa: B018
    with pytest.raises(AdminSchemaError):
        store.add("y", "admin", "long-enough-pass")
    assert path.read_bytes() == before


def test_an_unknown_role_or_name_opens_nothing(tmp_path: Path) -> None:
    owner = _admin(_client(tmp_path))
    store = AdminStore(tmp_path / "state" / "admin.json")
    assert store.identify("boss", PASSWORD) == "owner" and store.identify("nobody", PASSWORD) is None
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["operators"][0]["role"] = "emperor"
    store.path.write_text(json.dumps(data), encoding="utf-8")
    assert store.identify("boss", PASSWORD) is None  # a role this version does not know is no role
    assert owner  # the client is only there to have made the file


# The command line, for an owner who lost the password.


def test_the_command_line_lists_and_resets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _admin(_client(tmp_path))
    config = tmp_path / "state" / "config.toml"
    assert main(["operator", "list", "--config", str(config)]) == 0
    assert capsys.readouterr().out.split() == ["boss", "owner"]
    monkeypatch.setattr("sys.stdin", io.StringIO("typed-new-password-1\n"))
    assert main(["operator", "reset", "boss", "--config", str(config)]) == 0
    printed = capsys.readouterr()
    assert "typed-new-password-1" not in printed.out + printed.err  # a password is typed, never shown
    store = AdminStore(tmp_path / "state" / "admin.json")
    assert store.identify("boss", "typed-new-password-1") == "owner" and store.identify("boss", PASSWORD) is None
    monkeypatch.setattr("sys.stdin", io.StringIO("typed-new-password-1\n"))
    assert main(["operator", "reset", "nobody", "--config", str(config)]) == 1
    assert "no such operator" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert main(["operator", "reset", "boss", "--config", str(config)]) == 1
    assert "at least 10 characters" in capsys.readouterr().err
    assert store.identify("boss", "typed-new-password-1") == "owner"  # a refused password changes nothing


def test_the_command_line_asks_twice_on_a_terminal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _admin(_client(tmp_path))
    config = tmp_path / "state" / "config.toml"
    monkeypatch.setattr("sys.stdin", type("Tty", (io.StringIO,), {"isatty": lambda self: True})())
    answers = iter(["terminal-password-1", "terminal-password-2", "terminal-password-1", "terminal-password-1"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert main(["operator", "reset", "boss", "--config", str(config)]) == 1  # they differ
    assert "differ" in capsys.readouterr().err
    assert main(["operator", "reset", "boss", "--config", str(config)]) == 0
    assert AdminStore(tmp_path / "state" / "admin.json").identify("boss", "terminal-password-1") == "owner"


def test_the_command_line_says_when_there_is_no_operator_yet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["operator", "list", "--config", str(tmp_path / "config.toml")]) == 1
    assert "no operator yet" in capsys.readouterr().err
