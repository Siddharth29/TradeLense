from __future__ import annotations

"""Daily metrics rollup job.

This coroutine aggregates recent agent metrics and persists a daily snapshot
into the ``daily_metrics`` collection. Results are idempotent by date through
the ``client_order_id`` field which encodes the day and metric name.
"""

from datetime import date, datetime, time, timezone, timedelta
from typing import Sequence, Mapping

from packages.db import models
from packages.db.repositories import (
    aggregate_agent_metrics,
    insert_daily_metrics,
)

__all__ = ["run"]


async def run(
    day: date | None = None,
    *,
    dry_run: bool = False,
    assets: Sequence[str] | None = None,
) -> list[models.DailyMetricDoc]:
    """Aggregate and store daily metrics.

    Parameters
    ----------
    day:
        Day for which metrics should be recorded. Defaults to ``date.today()``.
    dry_run:
        If ``True`` the metrics are computed but not written to the database.
    assets:
        Optional sequence of asset identifiers. When omitted a default ``"ALL"``
        asset is used.
    """

    assets = list(assets) if assets else ["ALL"]
    day = day or date.today()
    ts = datetime.combine(day, time.min, tzinfo=timezone.utc)

    docs: list[models.DailyMetricDoc] = []
    for asset in assets:
        metrics: Mapping[str, float] = await aggregate_agent_metrics(asset, timedelta(days=1))
        for name, value in metrics.items():
            if value is None:
                continue
            cid = f"{day.isoformat()}:{asset}:{name}"
            docs.append(
                models.DailyMetricDoc(
                    timestamp=ts,
                    client_order_id=cid,
                    checksum=cid,
                    metric=f"{asset}.{name}",
                    value=float(value),
                )
            )

    if not dry_run and docs:
        await insert_daily_metrics(docs)
    return docs
