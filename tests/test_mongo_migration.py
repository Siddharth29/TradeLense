import pytest

from core import mongo_utils


@pytest.mark.asyncio
async def test_save_backtest_report_async_calls_repo(monkeypatch):
    captured = {}

    async def fake_insert(doc):
        captured['doc'] = doc

    monkeypatch.setattr(mongo_utils.repo, 'insert_backtest_report', fake_insert)
    await mongo_utils.save_backtest_report_async('run_async', [])
    assert captured['doc']['test_name'] == 'run_async'


@pytest.mark.asyncio
async def test_save_trades_async_calls_repo(monkeypatch):
    captured = {}

    async def fake_insert(docs):
        captured['docs'] = docs

    monkeypatch.setattr(mongo_utils.repo, 'insert_backtest_trades', fake_insert)
    count = await mongo_utils.save_trades_async('run_async', [{'result': 'win'}])
    assert count == 1
    assert captured['docs'][0]['test_name'] == 'run_async'


def test_deprecated_wrappers_call_async(monkeypatch):
    calls: dict[str, str] = {}

    async def fake_report(name, trades):  # pragma: no cover - simple capture
        calls['report'] = name

    async def fake_trades(name, trades):  # pragma: no cover - simple capture
        calls['trades'] = name
        return 0

    monkeypatch.setattr(mongo_utils, 'save_backtest_report_async', fake_report)
    monkeypatch.setattr(mongo_utils, 'save_trades_async', fake_trades)

    with pytest.warns(DeprecationWarning):
        mongo_utils.save_backtest_report('legacy', [])
    with pytest.warns(DeprecationWarning):
        mongo_utils.save_trades('legacy', [])

    assert calls['report'] == 'legacy'
    assert calls['trades'] == 'legacy'
