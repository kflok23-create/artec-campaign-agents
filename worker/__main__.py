"""Entry point: `python -m worker [run|smoke|status|index|search|search-check|assets]`."""

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
    index = sub.add_parser("index", help="index the Drive asset bank now")
    index.add_argument("--no-tag", action="store_true", help="sync files only; no vision calls")
    index.add_argument("--retag-failed", action="store_true", help="also retry failed tagging")
    index.add_argument("--tag-limit", type=int, help="tag at most this many images")
    search = sub.add_parser("search", help="search the asset index")
    search.add_argument("query")
    search.add_argument("--kind", choices=["image", "video"])
    search.add_argument("--safe-only", action="store_true", help="hide uncleared children")
    search.add_argument("--limit", type=int, default=5)
    check = sub.add_parser("search-check", help="run the gate's test queries (evals/)")
    check.add_argument("--limit", type=int, default=5)
    sub.add_parser("assets", help="summarise the asset index")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    if args.cmd == "status":
        print(anyio.run(status, settings))
        return 0
    if args.cmd == "smoke":
        print(anyio.run(smoke_once, settings, args.force))
        return 0
    if args.cmd in ("index", "search", "search-check", "assets"):
        from worker import commands

        if args.cmd == "index":
            out = anyio.run(
                lambda: commands.index_now(
                    settings,
                    tag=not args.no_tag,
                    retag_failed=args.retag_failed,
                    tag_limit=args.tag_limit,
                )
            )
        elif args.cmd == "search":
            out = anyio.run(
                lambda: commands.search(
                    settings,
                    args.query,
                    kind=args.kind,
                    safe_only=args.safe_only,
                    limit=args.limit,
                )
            )
        elif args.cmd == "search-check":
            out = anyio.run(lambda: commands.search_check(settings, limit=args.limit))
        else:
            out = anyio.run(commands.assets_summary, settings)
        print(out)
        return 0
    try:
        anyio.run(serve, settings)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
