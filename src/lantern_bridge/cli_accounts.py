# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""`lantern-bridge account ...` and `lantern-bridge device ...`: the accounts and the consoles from a terminal.

For an installation without a browser (or a script): the same store as the pages (``accounts.sqlite3``), the same
presets and the same journal, with the command line as the actor. The bridge may be running. Every command can
print JSON (``--json``, ``schema`` 1) instead of a table; nothing printed holds a token or its hash.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

#: What the journal shows as the actor of a line made here; an operator's name cannot look like it.
ACTOR = "(command line)"


def add_parsers(commands: Any, default_config: Path, config_help: str) -> list[argparse.ArgumentParser]:
    """Adds `account` and `device` to the command line; returns the parsers that take ``--config``."""
    from lantern_bridge.admin import presets

    leaves: list[argparse.ArgumentParser] = []

    def leaf(actions: Any, name: str, help_text: str, *, json_output: bool = False) -> argparse.ArgumentParser:
        parser: argparse.ArgumentParser = actions.add_parser(name, help=help_text)
        parser.add_argument("--config", type=Path, default=default_config, help=config_help)
        if json_output:
            parser.add_argument("--json", action="store_true", help="print JSON (schema 1) instead of a table")
        leaves.append(parser)
        return parser

    account = commands.add_parser("account", help="list, add, freeze or delete the accounts of the players")
    actions = account.add_subparsers(dest="action", required=True)
    leaf(actions, "list", "the accounts, their rights and how many consoles each has", json_output=True)
    create = leaf(actions, "create", "add an account with a preset of rights", json_output=True)
    create.add_argument("name", help="letters, digits, - _ and . (64 at most)")
    create.add_argument("--preset", choices=sorted(presets.PRESETS), default="full", help="default: full")
    create.add_argument("--age", help=f"an age limit (a child's account takes {presets.CHILD_AGE} when left out)")
    create.add_argument("--hide-unrated", action="store_true", help="with an age limit: hide what has no rating")
    for action, text in (("freeze", "refuse every console of an account, for now"), ("unfreeze", "lift the freeze")):
        leaf(actions, action, text).add_argument("name")
    delete = leaf(actions, "delete", "delete an account with its consoles, invitations and what is kept for it")
    delete.add_argument("name")
    delete.add_argument("--yes", action="store_true", help="confirm: there is no undoing it")

    device = commands.add_parser("device", help="list, suspend or revoke the consoles")
    actions = device.add_subparsers(dest="action", required=True)
    listing = leaf(actions, "list", "the consoles (paired devices), with their ids", json_output=True)
    listing.add_argument("--account", help="only the consoles of this account")
    for action, text in (
        ("revoke", "take a console off: it has to pair again"),
        ("suspend", "refuse a console for now, kept paired"),
        ("resume", "lift the suspension of a console"),
    ):
        leaf(actions, action, text).add_argument("id", help="the console's id, as `device list` shows it")
    return leaves


def run(args: argparse.Namespace) -> int:
    from lantern.server import SqliteAccounts
    from lantern.server.accounts import AccountError

    state = args.config.resolve().parent
    try:
        with SqliteAccounts(state / "accounts.sqlite3") as accounts:
            if args.command == "account":
                return _account(args, accounts, state)
            return _device(args, accounts, state)
    except AccountError as error:
        print(f"lantern-bridge: {error.detail}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"lantern-bridge: {error}", file=sys.stderr)
        return 1


def _note(state: Path, kind: str, subject: str = "", detail: str = "") -> None:
    from lantern_bridge.history import History

    history = History(state / "activity.sqlite3")
    history.event(kind, subject, detail, actor=ACTOR)
    history.close()


def _print_json(document: dict[str, Any]) -> None:
    print(json.dumps({"schema": 1, **document}, indent=2, ensure_ascii=False))


def _cap_text(cap: Any) -> str:
    if cap is None:
        return "-"
    return f"{cap.age}+" + (" hide unrated" if cap.unrated == "hide" else "") if cap.age is not None else "set"


def _account_doc(user: Any, devices: int) -> dict[str, Any]:
    from lantern_bridge.admin import presets

    return {
        "name": user.name,
        "state": user.state,
        "preset": presets.preset_of(user.scopes),
        "scopes": list(user.scopes),
        "rating_cap": user.rating_cap.to_json() if user.rating_cap is not None else None,
        "devices": devices,
        "created": user.created,
    }


def _account(args: argparse.Namespace, accounts: Any, state: Path) -> int:
    from lantern.server.accounts import AccountError

    from lantern_bridge.admin import presets

    if args.action == "list":
        counts: dict[str, int] = {}
        for found in accounts.devices():
            counts[found.user] = counts.get(found.user, 0) + 1
        docs = [_account_doc(u, counts.get(u.name, 0)) for u in accounts.users()]
        if args.json:
            _print_json({"accounts": docs})
        else:
            for doc in docs:
                limit = _cap_text(accounts.user(doc["name"]).rating_cap)
                print("\t".join((doc["name"], doc["state"], doc["preset"], limit, f"{doc['devices']} console(s)")))
        return 0
    if args.action == "create":
        cap, problem = presets.cap_from(args.preset, args.age or "", args.hide_unrated)
        if problem is not None:
            print("lantern-bridge: an age is a number from 0 to 99", file=sys.stderr)
            return 1
        user = accounts.create_user(args.name, presets.PRESETS[args.preset])
        if cap is not None:
            user = accounts.update_user(args.name, rating_cap=cap)
        _note(state, "account_added", user.name)
        if args.json:
            _print_json({"account": _account_doc(user, 0)})
        else:
            print(f"The account {user.name} is added ({args.preset}).")
        return 0
    if accounts.user(args.name) is None:
        raise AccountError(404, "not_found", "No such account.")
    if args.action in ("freeze", "unfreeze"):
        new = "frozen" if args.action == "freeze" else "active"
        accounts.update_user(args.name, state=new)
        _note(state, "account_changed", args.name, new)
        print(f"The account {args.name} is {new}.")
        return 0
    if not args.yes:
        print(
            "lantern-bridge: this deletes the account with its consoles, invitations and what is kept for it; "
            "add --yes to confirm",
            file=sys.stderr,
        )
        return 1
    _delete(args, state)
    return 0


def _delete(args: argparse.Namespace, state: Path) -> None:
    """Deletes through the server's own code, so that the saves, requests and sync data of the account go too."""
    import dataclasses

    from lantern.server import LanternApp, SqliteAccounts

    from lantern_bridge.config import AerofoilSource, load, save
    from lantern_bridge.runtime import Runtime
    from lantern_bridge.upstream import BridgeLibrary

    config = load(args.config)
    library = BridgeLibrary(base="/api/lantern")
    runtime = Runtime(config, state, library)
    try:
        with SqliteAccounts(state / "accounts.sqlite3") as accounts:
            lantern = LanternApp(
                library,
                uid=config.uid,
                name=config.name,
                accounts=accounts,
                public=config.public,
                saves=runtime.saves,
                encrypted_saves=False,
                requests=runtime.requests,
            )
            lantern.delete_account(args.name)
    finally:
        runtime.close()
    aero = next((s for s in config.sources if isinstance(s, AerofoilSource)), None)
    if aero is not None and any(x.account == args.name for x in aero.logins):
        kept = tuple(x for x in aero.logins if x.account != args.name)
        config.sources[config.sources.index(aero)] = dataclasses.replace(aero, logins=kept)
        save(args.config, config)  # the bridge reads it again when its settings are saved or it restarts
    _note(state, "account_deleted", args.name)
    print(f"The account {args.name} is deleted with its consoles and invitations.")


def _device_doc(found: Any) -> dict[str, Any]:
    return {
        "id": found.id,
        "account": found.user,
        "name": found.device,
        "scopes": list(found.scopes),
        "rating_cap": found.rating_cap.to_json() if found.rating_cap is not None else None,
        "paired": found.created,
        "last_used": found.last_used,
        "expires": found.expires,
        "bound": found.bound,
        "suspended": found.suspended,
    }


def _device(args: argparse.Namespace, accounts: Any, state: Path) -> int:
    from lantern.server.accounts import AccountError

    if args.action == "list":
        docs = [_device_doc(d) for d in accounts.devices(args.account)]
        if args.json:
            _print_json({"devices": docs})
        else:
            for doc in docs:
                state_text = "suspended" if doc["suspended"] else "active"
                print("\t".join((doc["id"], doc["account"], doc["name"], state_text, doc["last_used"] or "-")))
        return 0
    found = next((d for d in accounts.devices() if d.id == args.id), None)
    if found is None:
        raise AccountError(404, "not_found", "No such console.")
    if args.action == "revoke":
        accounts.revoke(found.id)
        _note(state, "revoked", found.user, found.device[:64])
        print(f"The console {found.device} of {found.user} is revoked.")
        return 0
    suspended = args.action == "suspend"
    accounts.update_device(found.id, suspended=suspended)
    _note(state, "device_changed", found.user, f"{found.device[:48]}: {'suspended' if suspended else 'active'}")
    print(f"The console {found.device} of {found.user} is {'suspended' if suspended else 'active again'}.")
    return 0
