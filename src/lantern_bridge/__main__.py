"""The command line: `lantern-bridge` or `python -m lantern_bridge`."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from lantern_bridge import SPEC_DRAFT, __version__
from lantern_bridge.config import ConfigError, load_or_create


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
    serve.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("LANTERN_BRIDGE_CONFIG", "config.toml")),
        help="the configuration file, created with a new uid when absent (default: ./config.toml)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
    uvicorn.run(make_app(name=config.name, uid=config.uid), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
