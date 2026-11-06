"""The command line: `lantern-bridge` or `python -m lantern_bridge`."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from lantern_bridge import SPEC_DRAFT, __version__
from lantern_bridge.config import ConfigError, load_or_create

_DEFAULT_CONFIG = Path(os.environ.get("LANTERN_BRIDGE_CONFIG", "config.toml"))
_CONFIG_HELP = "the configuration file, created with a new uid when absent (default: ./config.toml)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lantern-bridge",
        description="Present a Tinfoil, Ownfoil or AeroFoil library, or plain folders, as a Lantern server.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__} (Lantern draft {SPEC_DRAFT})")
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="run the server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--config", type=Path, default=_DEFAULT_CONFIG, help=_CONFIG_HELP)
    status = commands.add_parser("status", help="show the sources and the hashing progress")
    rehash = commands.add_parser("rehash", help="compute the hashes of a source (or of all) again")
    rehash.add_argument("source", nargs="?", help="a source id (default: all)")
    invite = commands.add_parser("invite", help="print a code that pairs a new device")
    invite.add_argument("--user", default="family", help="the account the device joins (made if absent)")
    invite.add_argument("--devices", type=int, default=1, help="how many devices may use the code")
    invite.add_argument("--hours", type=int, default=24, help="how long the code is good for")
    invite.add_argument("--admin", action="store_true", help="give the account every scope, administration included")
    for command in (status, rehash, invite):
        command.add_argument("--config", type=Path, default=_DEFAULT_CONFIG, help=_CONFIG_HELP)
    return parser


def _inspect(args: argparse.Namespace) -> int:
    from lantern_bridge.config import load
    from lantern_bridge.index import Index

    state = args.config.resolve().parent
    index_file = state / "index.sqlite3"
    try:
        config = load(args.config)
    except ConfigError as error:
        print(f"lantern-bridge: {error}", file=sys.stderr)
        return 1
    if not index_file.exists():
        print("No index yet: start the server first.", file=sys.stderr)
        return 1
    with Index(index_file) as index:
        if args.command == "rehash":
            known = {source.id for source in config.sources}
            if args.source is not None and args.source not in known:
                print(
                    f"lantern-bridge: no source {args.source!r} (known: {', '.join(sorted(known)) or 'none'})",
                    file=sys.stderr,
                )
                return 1
            print(f"{index.rehash(args.source)} hashes forgotten; the server computes them again.")
            return 0
        counts = index.counts_by_source()
        for source in config.sources:
            c = counts.get(source.id, {"hashed": 0, "pending": 0})
            print(f"{source.id}\t{source.path}\t{c['hashed']} hashed\t{c['pending']} pending")
        if not config.sources:
            print("No source configured.")
    return 0


def _invite(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime, timedelta

    from lantern.server import SqliteAccounts
    from lantern.server.accounts import SCOPES, utc

    if args.devices < 1 or args.hours < 1:
        print("lantern-bridge: --devices and --hours are at least 1", file=sys.stderr)
        return 1
    accounts_file = args.config.resolve().parent / "accounts.sqlite3"
    scopes = SCOPES if args.admin else ("library", "saves", "cheats", "requests", "sync.read", "sync.write")
    with SqliteAccounts(accounts_file) as accounts:
        user = accounts.user(args.user) or accounts.create_user(args.user, scopes)
        expires = utc(datetime.now(UTC) + timedelta(hours=args.hours))
        invitation = accounts.create_invitation(user.name, scopes, expires, args.devices)
    print(
        f"Pairing code for {user.name}: {invitation.code} (good until {invitation.expires}, {args.devices} device(s))"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"status", "rehash"}:
        return _inspect(args)
    if args.command == "invite":
        return _invite(args)
    if args.command != "serve":
        build_parser().print_help()
        return 0
    import uvicorn

    from lantern_bridge.app import make_app

    try:
        config = load_or_create(args.config)
    except ConfigError as error:
        print(f"lantern-bridge: {error}", file=sys.stderr)
        return 1
    uvicorn.run(make_app(config, args.config.resolve().parent), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
