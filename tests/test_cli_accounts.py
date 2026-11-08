# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""`lantern-bridge account ...` and `device ...`: the accounts and the consoles from a terminal."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from lantern.server import SqliteAccounts

from lantern_bridge.__main__ import main
from lantern_bridge.history import History


def _run(tmp_path: Path, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main([*argv[:2], "--config", str(tmp_path / "config.toml"), *argv[2:]])
    out = capsys.readouterr()
    return code, out.out, out.err


def _journal(tmp_path: Path) -> list[tuple[str, str, str, str]]:
    history = History(tmp_path / "activity.sqlite3")
    rows = [(e.kind, e.subject, e.detail, e.actor) for e in history.events()]
    history.close()
    return rows


def test_an_account_is_added_with_a_preset_listed_frozen_and_lifted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path, capsys, "account", "create", "lea", "--preset", "child", "--age", "10")[0] == 0
    assert _run(tmp_path, capsys, "account", "create", "zoe", "--preset", "guest")[0] == 0
    code, out, _ = _run(tmp_path, capsys, "account", "list", "--json")
    document = json.loads(out)
    assert code == 0 and document["schema"] == 1
    lea, zoe = sorted(document["accounts"], key=lambda a: a["name"])
    assert lea["preset"] == "child" and lea["rating_cap"] == {"age": 10} and "cheats" not in lea["scopes"]
    assert zoe["preset"] == "guest" and zoe["scopes"] == ["library"] and zoe["rating_cap"] is None
    assert _run(tmp_path, capsys, "account", "freeze", "lea")[0] == 0
    code, table, _ = _run(tmp_path, capsys, "account", "list")
    assert code == 0 and "lea\tfrozen\tchild\t10+" in table
    assert _run(tmp_path, capsys, "account", "unfreeze", "lea")[0] == 0
    with SqliteAccounts(tmp_path / "accounts.sqlite3") as accounts:
        assert accounts.user("lea").state == "active"  # type: ignore[union-attr]
    kinds = [(k, s, d, a) for k, s, d, a in _journal(tmp_path)]
    assert ("account_changed", "lea", "frozen", "(command line)") in kinds
    assert ("account_added", "zoe", "", "(command line)") in kinds


def test_the_command_refuses_what_the_pages_refuse(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(tmp_path, capsys, "account", "create", "lea")[0] == 0
    code, _, err = _run(tmp_path, capsys, "account", "create", "lea")
    assert code == 1 and "exists" in err
    assert _run(tmp_path, capsys, "account", "create", "bad name")[0] == 1
    assert _run(tmp_path, capsys, "account", "create", "kid", "--preset", "child", "--age", "abc")[0] == 1
    code, _, err = _run(tmp_path, capsys, "account", "freeze", "nobody")
    assert code == 1 and "No such account" in err
    assert _run(tmp_path, capsys, "device", "revoke", "nope")[0] == 1


def test_a_console_is_listed_suspended_and_revoked_without_ever_showing_its_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with SqliteAccounts(tmp_path / "accounts.sqlite3") as accounts:
        accounts.create_user("lea", ["library", "saves"])
        token, device = accounts.issue("lea", "Living room", ["library", "saves"])
    code, out, _ = _run(tmp_path, capsys, "device", "list", "--json")
    assert code == 0 and token not in out and device.token_hash not in out
    listed = json.loads(out)["devices"]
    assert [(d["id"], d["account"], d["name"], d["suspended"]) for d in listed] == [
        (device.id, "lea", "Living room", False)
    ]
    assert json.loads(_run(tmp_path, capsys, "device", "list", "--account", "zoe", "--json")[1])["devices"] == []
    assert _run(tmp_path, capsys, "device", "suspend", device.id)[0] == 0
    with SqliteAccounts(tmp_path / "accounts.sqlite3") as accounts:
        assert accounts.device_of(token).suspended is True  # type: ignore[union-attr]
    assert "suspended" in _run(tmp_path, capsys, "device", "list")[1]
    assert _run(tmp_path, capsys, "device", "resume", device.id)[0] == 0
    assert _run(tmp_path, capsys, "device", "revoke", device.id)[0] == 0
    with SqliteAccounts(tmp_path / "accounts.sqlite3") as accounts:
        assert accounts.device_of(token) is None
    kinds = [(k, s, a) for k, s, _, a in _journal(tmp_path)]
    assert ("revoked", "lea", "(command line)") in kinds and ("device_changed", "lea", "(command line)") in kinds


def test_an_account_is_deleted_only_when_confirmed_and_takes_its_consoles_along(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with SqliteAccounts(tmp_path / "accounts.sqlite3") as accounts:
        accounts.create_user("lea", ["library"])
        token, _ = accounts.issue("lea", "Living room", ["library"])
    code, _, err = _run(tmp_path, capsys, "account", "delete", "lea")
    assert code == 1 and "--yes" in err
    with SqliteAccounts(tmp_path / "accounts.sqlite3") as accounts:
        assert accounts.user("lea") is not None
    assert _run(tmp_path, capsys, "account", "delete", "lea", "--yes")[0] == 0
    with SqliteAccounts(tmp_path / "accounts.sqlite3") as accounts:
        assert accounts.user("lea") is None and accounts.device_of(token) is None
    assert ("account_deleted", "lea", "", "(command line)") in _journal(tmp_path)
