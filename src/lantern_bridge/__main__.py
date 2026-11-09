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
    serve.add_argument("--json-logs", action="store_true", help="write the logs as JSON lines (for a container)")
    status = commands.add_parser("status", help="show the sources and the hashing progress")
    rehash = commands.add_parser("rehash", help="compute the hashes of a source (or of all) again")
    rehash.add_argument("source", nargs="?", help="a source id (default: all)")
    rehash.add_argument("--title", help="only the files of this title id (to test with one game)")
    invite = commands.add_parser("invite", help="print a code that pairs a new device")
    invite.add_argument("--user", default="family", help="the account the device joins (made if absent)")
    invite.add_argument("--devices", type=int, default=1, help="how many devices may use the code")
    invite.add_argument("--hours", type=int, default=24, help="how long the code is good for")
    invite.add_argument("--admin", action="store_true", help="give the account every scope, administration included")
    backup = commands.add_parser("backup", help="write a backup of the settings and the index (the bridge may run)")
    backup.add_argument("-o", "--output", type=Path, help="the file to write (default: lantern-bridge-backup.zip)")
    restore = commands.add_parser("restore", help="restore a backup (stop the bridge first)")
    restore.add_argument("file", type=Path)
    aero_key = commands.add_parser(
        "aerofoil-key",
        help="make the key pair for an AeroFoil whose index is encrypted for the bridge (see the README)",
    )
    aero_key.add_argument(
        "-o", "--output", type=Path, default=Path("aerofoil-index.pem"), help="the private key to write"
    )
    operator = commands.add_parser("operator", help="list the operators of the pages, or give one a new password")
    actions = operator.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list", help="the operators and their roles")
    reset = actions.add_parser(
        "reset", help="set an operator's password, typed twice (or one line on the standard input; the bridge may run)"
    )
    reset.add_argument("name", help="the operator's name")
    for command in (status, rehash, invite, backup, restore, listing, reset):
        command.add_argument("--config", type=Path, default=_DEFAULT_CONFIG, help=_CONFIG_HELP)
    from lantern_bridge import cli_accounts

    cli_accounts.add_parsers(commands, _DEFAULT_CONFIG, _CONFIG_HELP)
    return parser


def _inspect(args: argparse.Namespace) -> int:
    from lantern.server.file_index import SqliteIndex

    from lantern_bridge.config import load

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
    with SqliteIndex(index_file) as index:
        if args.command == "rehash":
            known = {source.id for source in config.sources}
            if args.source is not None and args.source not in known:
                print(
                    f"lantern-bridge: no source {args.source!r} (known: {', '.join(sorted(known)) or 'none'})",
                    file=sys.stderr,
                )
                return 1
            if args.title:
                from lantern_bridge.identify import identify

                wanted = args.title.upper()
                files = [
                    (row.source, row.path)
                    for row in index.rows(args.source)
                    if (identity := identify(row.path)) is not None and identity.title_id == wanted
                ]
                if not files:
                    print(f"lantern-bridge: no file of the title {wanted} in the index", file=sys.stderr)
                    return 1
                print(f"{index.rehash_files(files)} hashes of {wanted} forgotten; the server computes them again.")
                return 0
            print(f"{index.rehash(args.source)} hashes forgotten; the server computes them again.")
            return 0
        counts = index.counts_by_source()
        for source in config.sources:
            c = counts.get(source.id, {"hashed": 0, "pending": 0})
            print(f"{source.id}\t{source.location}\t{c['hashed']} hashed\t{c['pending']} pending")
        if not config.sources:
            print("No source configured.")
    return 0


def _note_restore(state: Path, names: list[str]) -> None:
    """Puts the restoration in the journal (the restored one, when the archive held it)."""
    from lantern_bridge.history import History

    history = History(state / "activity.sqlite3")
    history.event("restored", detail=", ".join(names))
    history.close()


def _backup(args: argparse.Namespace) -> int:
    from lantern_bridge import backup

    config = args.config.resolve()
    try:
        if args.command == "backup":
            out = (args.output or Path("lantern-bridge-backup.zip")).resolve()
            names = backup.make(config.parent, config, out)
            print(f"{out}: {', '.join(names)}")
        else:
            names = backup.restore(args.file, config.parent, config)
            _note_restore(config.parent, names)
            print(f"restored {', '.join(names)}; what was replaced is kept beside it as .before-restore")
    except (backup.BackupError, ConfigError, OSError) as error:
        print(f"lantern-bridge: {error}", file=sys.stderr)
        return 1
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


def _typed_password() -> str | None:
    """The new password: typed twice on a terminal (never shown), or one line on the standard input."""
    import getpass

    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("New password: ")
    if first != getpass.getpass("Again: "):
        print("lantern-bridge: the two passwords differ", file=sys.stderr)
        return None
    return first


def _operator(args: argparse.Namespace) -> int:
    from lantern_bridge.admin.auth import MIN_LENGTH, AdminSchemaError, AdminStore

    file = args.config.resolve().parent / "admin.json"
    if not file.exists():
        print("lantern-bridge: no operator yet: open the pages, the setup page gives the code", file=sys.stderr)
        return 1
    try:
        store = AdminStore(file)
        if args.action == "list":
            for found in store.operators():
                print(f"{found.name}\t{found.role}")
            return 0
        if args.name not in {found.name for found in store.operators()}:
            print("lantern-bridge: no such operator", file=sys.stderr)
            return 1
        if (typed := _typed_password()) is None:
            return 1
        if len(typed) < MIN_LENGTH:
            print(f"lantern-bridge: a password has at least {MIN_LENGTH} characters", file=sys.stderr)
            return 1
        refused = store.set_password(args.name, typed) is not None
    except (AdminSchemaError, OSError) as error:
        print(f"lantern-bridge: {error}", file=sys.stderr)
        return 1
    if refused:
        print("lantern-bridge: the password could not be set", file=sys.stderr)
        return 1
    print(f"The password of {args.name} is set.")
    print("Sessions already open stay open until they end; restart the bridge to close them now.")
    return 0


def _aerofoil_key(args: argparse.Namespace) -> int:
    from lantern_bridge.sources import shopkey

    private, public = shopkey.generate()
    try:
        shopkey.write_private(args.output, private)
    except FileExistsError:
        print(f"lantern-bridge: {args.output} exists; it is not overwritten", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"lantern-bridge: {args.output} cannot be written ({error.strerror})", file=sys.stderr)
        return 1
    print(f"The private key is in {args.output} (readable by you only): keep it, and out of any backup you share.")
    print("Put this public key in AeroFoil, under Settings > Shop > Public key, with encryption on:\n")
    print(public.decode())
    print(f'\nThen give the source `index_key_file = "{args.output}"` in the configuration.')
    print("Tinfoil can no longer read that AeroFoil: the key is one for the whole shop.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "aerofoil-key":
        return _aerofoil_key(args)
    if args.command in {"status", "rehash"}:
        return _inspect(args)
    if args.command == "invite":
        return _invite(args)
    if args.command in {"backup", "restore"}:
        return _backup(args)
    if args.command == "operator":
        return _operator(args)
    if args.command in {"account", "device"}:
        from lantern_bridge import cli_accounts

        return cli_accounts.run(args)
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
    uvicorn.run(
        make_app(config, args.config.resolve().parent, json_logs=args.json_logs, config_path=args.config.resolve()),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
