import pandas as pd
from datetime import datetime
from core.trigger_check import (
    entry_trigger_met,
    _chk_retest,
    _chk_breakout,
    _chk_fvg_fill,
    _chk_liq_sweep,
    _chk_range_retest,
)

# All checkers hard-fail on buffer == 0.0 ("no fallbacks in production") and
# take an asset for logging — tests pass explicit buffers like live preds do.
ASSET = "TESTASSET"


def _make_df(vals):
    base = datetime(2024, 1, 1, 9, 20)
    rows = [
        {"open": o, "high": h, "low": l, "close": c}
        for o, h, l, c in vals
    ]
    return pd.DataFrame(rows, index=[base + pd.Timedelta(minutes=5 * i) for i in range(len(rows))])


def test_retest():
    fut = _make_df([(0, 5, -1, 2)])
    assert _chk_retest(0, fut, 1.0, ASSET)


def test_retest_buffer():
    fut = _make_df([(506, 510, 505, 507)])
    assert not _chk_retest(500, fut, 1.0, ASSET)
    assert _chk_retest(500, fut, 10.0, ASSET)


def test_breakout():
    fut = _make_df([(0, 5, -1, 3)])
    assert _chk_breakout(1, "long", fut, 0.5, ASSET)


def test_fvg_fill():
    fut = _make_df([(0, 5, -1, 2)])
    assert _chk_fvg_fill((1, 4), fut, 0.5)


def test_fvg_fill_buffer():
    fut = _make_df([(522, 524, 522, 523)])
    assert not _chk_fvg_fill((500, 520), fut, 0.5)
    assert _chk_fvg_fill((500, 520), fut, 5.0)


def test_liq_sweep():
    fut = _make_df([(5, 6, 0, 6)])
    assert _chk_liq_sweep(1, "long", fut, 0.5, ASSET)


def test_range_retest():
    fut = _make_df([(0, 5, -1, 5), (4, 6, 1, 3)])
    assert _chk_range_retest(5, "long", fut, 0.5, ASSET)


def test_entry_router():
    fut = _make_df([(0, 5, -1, 3)])
    pred = {"direction": "long", "condition": "breakout", "level": 1}
    assert entry_trigger_met(pred, fut, 0.5, ASSET)


def test_entry_fvg_float_level():
    fut = _make_df([(0, 5, -1, 3)])
    pred = {"direction": "long", "condition": "fvg fill", "level": 2}
    assert entry_trigger_met(pred, fut, 0.5, ASSET)


def test_entry_retest_buffer():
    fut = _make_df([(506, 510, 505, 507)])
    pred = {"direction": "long", "condition": "retest", "level": 500, "buffer": 10}
    assert entry_trigger_met(pred, fut, 1.0, ASSET)
