"""The command line: `lantern-bridge` or `python -m lantern_bridge`."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from lantern.uid import check_uid, new_uid

from lantern_bridge import SPEC_DRAFT, __version__


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
    serve.add_argument("--name", default="Lantern bridge", help="what clients show")
    serve.add_argument("--uid", help="the server's UUID, which never changes (default: a new one, kept nowhere yet)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "serve":
        build_parser().print_help()
        return 0
    import uvicorn

    from lantern_bridge.app import make_app

    if args.uid is None:
        args.uid = new_uid()
        print(f"No --uid: using {args.uid}, which is not kept (the configuration comes later).", file=sys.stderr)
    uvicorn.run(make_app(name=args.name, uid=check_uid(args.uid)), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
