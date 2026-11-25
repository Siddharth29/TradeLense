from __future__ import annotations

"""Command line interface to compute daily metrics rollups."""

import argparse
import asyncio

from apps.backend.jobs import daily_metrics


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run daily metrics job")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute metrics without persisting to the database",
    )
    args = parser.parse_args(argv)
    asyncio.run(daily_metrics.run(dry_run=args.dry_run))


if __name__ == "__main__":  # pragma: no cover - script entry point
    main()
