from __future__ import annotations

"""CLI to generate replay JSONL data from stored trades and orders."""

import argparse
import asyncio
import json
from datetime import datetime
import sys

from packages.core import replay


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build replay JSONL")
    parser.add_argument("--asset", required=True, help="Asset symbol")
    parser.add_argument("--start", help="Start time (ISO format)")
    parser.add_argument("--end", help="End time (ISO format)")
    parser.add_argument("--output", help="File to write JSONL to (defaults to stdout)")
    args = parser.parse_args(argv)

    start = _parse_time(args.start)
    end = _parse_time(args.end)
    events = asyncio.run(replay.build_replay(args.asset, start=start, end=end))

    fh = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
    try:
        for event in events:
            print(json.dumps(event, default=str), file=fh)
    finally:
        if args.output:
            fh.close()


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
