"""Entry point: `python -m worker [run|smoke|status]`."""

from __future__ import annotations

import argparse
import sys

import anyio

from services.config import get_settings
from services.logs import configure_logging
from worker.main import serve, smoke_once, status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m worker")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="run the worker loop (default)")
    smoke = sub.add_parser("smoke", help="run the Phase 0 smoke call once")
    smoke.add_argument("--force", action="store_true", help="run even if this deployment passed")
    sub.add_parser("status", help="show recent agent runs and today's Claude spend")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    if args.cmd == "status":
        print(anyio.run(status, settings))
        return 0
    if args.cmd == "smoke":
        print(anyio.run(smoke_once, settings, args.force))
        return 0
    try:
        anyio.run(serve, settings)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
