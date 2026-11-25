from scripts import run_daily_metrics
from apps.backend.jobs import daily_metrics


def test_cli_invokes_job(monkeypatch):
    called = {}

    async def fake_run(*, dry_run: bool = False):
        called["dry_run"] = dry_run

    monkeypatch.setattr(daily_metrics, "run", fake_run)

    run_daily_metrics.main(["--dry-run"])
    assert called["dry_run"] is True

    called.clear()
    run_daily_metrics.main([])
    assert called["dry_run"] is False
