"""Candle retrieval and validation for offline replay.

Why this exists
---------------
``scripts/dl_full_window.py`` is a flat script: it pulls four symbols
(BTCUSD, XAUUSD, GBPUSD, USOIL), writes CSVs, and validates nothing. Three of
the six assets actually traded during the freeze window — **XAGUSD, EURUSD and
USTEC** — are missing from it entirely, and XAGUSD alone carries 75% of the
freeze-era loss. A sweep run on that data would silently skip the asset that
matters most.

This module replaces it with a source/validator/repository split so that:

- the *same* validated loader serves the MT5 pull and the offline sweep;
- a bad pull is caught at write time, not discovered as a weird sweep result;
- adding a rule or a source does not touch existing code.

SOLID
-----
- **S**  ``CandleSource`` fetches. ``CandleRule`` judges. ``CandleRepository``
  coordinates and persists. Three reasons to change, three types.
- **O**  New rules and new sources plug in via the protocols below. Neither the
  repository nor any existing rule is edited to add one.
- **L**  Every source honours the same ``fetch`` contract; every rule returns a
  ``RuleResult`` and never raises. They are freely substitutable.
- **I**  Each protocol is a single method — an MT5 source is not forced to know
  about CSV paths, a rule is not forced to know about I/O.
- **D**  The repository depends on the two protocols, never on MT5 or on the
  filesystem layout. That is what lets the sweep run on Linux with no broker.

Reuse over reinvention
----------------------
OHLC sanity and sequence/gap logic already exist and are already tuned to this
broker's session and maintenance windows (``candle_validation.py``,
``candles.is_expected_market_gap``). The rules here **wrap** them rather than
writing a second, subtly-different implementation that would drift.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Protocol, Sequence, runtime_checkable

import pandas as pd

from .candles import Candle, is_expected_market_gap
from .candle_validation import validate_candle_data_quality, validate_candle_sequence
from .logger import get_logger

logger = get_logger(__name__)

INTERVAL_MINUTES = {"5m": 5, "15m": 15, "1h": 60}


# ── Value objects ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CandleSpec:
    """What was asked for. Rules validate the result *against this*, which is
    what makes "the pull silently returned 3 days instead of 15" catchable.
    """

    symbol: str
    interval: str
    start: datetime
    end: datetime

    @property
    def interval_minutes(self) -> int:
        try:
            return INTERVAL_MINUTES[self.interval]
        except KeyError:
            raise ValueError(f"unknown interval {self.interval!r}") from None


@dataclass(frozen=True)
class RuleResult:
    ok: bool
    rule: str
    detail: str = ""


@dataclass
class ValidationReport:
    """Every rule's verdict, not just the first failure.

    Collecting all of them matters: a pull can be both short *and* gappy, and
    fixing one then re-running to discover the other wastes a broker session.
    """

    spec: CandleSpec
    results: List[RuleResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def failures(self) -> List[RuleResult]:
        return [r for r in self.results if not r.ok]

    def summary(self) -> str:
        if self.ok:
            return f"{self.spec.symbol} {self.spec.interval}: {len(self.results)} rules PASS"
        parts = "; ".join(f"{r.rule}: {r.detail}" for r in self.failures)
        return f"{self.spec.symbol} {self.spec.interval}: FAIL — {parts}"


# ── Protocols ───────────────────────────────────────────────────────────────

@runtime_checkable
class CandleSource(Protocol):
    """Where candles come from. Implementations own their own connection."""

    def fetch(self, spec: CandleSpec) -> List[Candle]: ...


@runtime_checkable
class CandleRule(Protocol):
    """One validation concern. MUST NOT raise — return a failing RuleResult."""

    name: str

    def check(self, candles: Sequence[Candle], spec: CandleSpec) -> RuleResult: ...


# ── Rules ───────────────────────────────────────────────────────────────────

class NonEmptyRule:
    name = "non_empty"

    def check(self, candles: Sequence[Candle], spec: CandleSpec) -> RuleResult:
        if not candles:
            return RuleResult(False, self.name, "zero candles returned")
        return RuleResult(True, self.name, f"{len(candles)} candles")


class OhlcSanityRule:
    """Per-candle OHLC integrity. Delegates to the existing validator."""

    name = "ohlc_sanity"

    def check(self, candles: Sequence[Candle], spec: CandleSpec) -> RuleResult:
        bad: List[str] = []
        for c in candles:
            ok, msg = validate_candle_data_quality(c)
            if not ok:
                bad.append(f"{c.time}: {msg}")
                if len(bad) >= 5:  # a systemic fault shows in the first few
                    break
        if bad:
            return RuleResult(False, self.name, f"{len(bad)}+ bad candles — " + " | ".join(bad))
        return RuleResult(True, self.name, "all OHLC relationships valid")


class MonotonicRule:
    """Timestamps strictly increasing — catches duplicates and out-of-order rows.

    Kept separate from the gap check because the fixes differ: duplicates mean
    a bad merge or a double-write, gaps mean a bad fetch window.
    """

    name = "monotonic"

    def check(self, candles: Sequence[Candle], spec: CandleSpec) -> RuleResult:
        dupes = 0
        backwards = 0
        for prev, curr in zip(candles, candles[1:]):
            if curr.time == prev.time:
                dupes += 1
            elif curr.time < prev.time:
                backwards += 1
        if dupes or backwards:
            return RuleResult(
                False, self.name, f"{dupes} duplicate timestamps, {backwards} out-of-order"
            )
        return RuleResult(True, self.name, "strictly increasing")


class SequenceRule:
    """Gap / alignment / closure. Delegates to the existing session-aware validator."""

    name = "sequence"

    def check(self, candles: Sequence[Candle], spec: CandleSpec) -> RuleResult:
        ok, msg = validate_candle_sequence(
            list(candles), spec.interval_minutes, check_gaps=True, asset=spec.symbol
        )
        return RuleResult(ok, self.name, msg or "no gaps")


class CoverageRule:
    """Does the data actually span the window that was requested?

    The failure this exists for: a broker returns the most recent N bars rather
    than the requested range, the CSV looks healthy, and the sweep quietly runs
    on the wrong fortnight.
    """

    name = "coverage"

    def __init__(self, tolerance: timedelta = timedelta(hours=6)) -> None:
        self._tol = tolerance

    def check(self, candles: Sequence[Candle], spec: CandleSpec) -> RuleResult:
        if not candles:
            return RuleResult(False, self.name, "no candles to cover the window")
        first = pd.Timestamp(candles[0].time).to_pydatetime()
        last = pd.Timestamp(candles[-1].time).to_pydatetime()
        first = first if first.tzinfo else first.replace(tzinfo=timezone.utc)
        last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)

        problems = []
        if first > spec.start + self._tol:
            problems.append(f"starts {first.isoformat()} (wanted {spec.start.isoformat()})")
        if last < spec.end - self._tol:
            problems.append(f"ends {last.isoformat()} (wanted {spec.end.isoformat()})")
        if problems:
            return RuleResult(False, self.name, "; ".join(problems))
        return RuleResult(True, self.name, f"{first.date()} -> {last.date()}")


class UnexpectedGapRule:
    """Gaps that session closure does not explain.

    ``SequenceRule`` is broad; this one isolates the gaps that are genuinely
    missing data (weekday holes) from the weekend and maintenance breaks that
    are supposed to be there, so a legitimate Friday-to-Sunday gap does not
    train the operator to ignore real failures.
    """

    name = "unexpected_gaps"

    def __init__(self, max_reported: int = 5) -> None:
        self._max = max_reported

    def check(self, candles: Sequence[Candle], spec: CandleSpec) -> RuleResult:
        step = timedelta(minutes=spec.interval_minutes)
        found: List[str] = []
        for prev, curr in zip(candles, candles[1:]):
            delta = pd.Timestamp(curr.time) - pd.Timestamp(prev.time)
            if delta <= step:
                continue
            if is_expected_market_gap(pd.Timestamp(prev.time), pd.Timestamp(curr.time), spec.symbol):
                continue
            found.append(f"{prev.time} -> {curr.time} ({delta})")
            if len(found) >= self._max:
                break
        if found:
            return RuleResult(False, self.name, f"{len(found)}+ unexplained — " + " | ".join(found))
        return RuleResult(True, self.name, "all gaps are session closures")


DEFAULT_RULES: tuple[CandleRule, ...] = (
    NonEmptyRule(),
    MonotonicRule(),
    OhlcSanityRule(),
    CoverageRule(),
    SequenceRule(),
    UnexpectedGapRule(),
)


class CandleValidator:
    """Runs a rule chain and reports every verdict."""

    def __init__(self, rules: Iterable[CandleRule] = DEFAULT_RULES) -> None:
        self._rules = tuple(rules)

    def validate(self, candles: Sequence[Candle], spec: CandleSpec) -> ValidationReport:
        report = ValidationReport(spec=spec)
        for rule in self._rules:
            try:
                report.results.append(rule.check(candles, spec))
            except Exception as exc:  # noqa: BLE001 — a broken rule is a failure, not a crash
                report.results.append(
                    RuleResult(False, getattr(rule, "name", "?"), f"rule raised {type(exc).__name__}: {exc}")
                )
        return report


# ── Sources ─────────────────────────────────────────────────────────────────

class CsvCandleSource:
    """Read candles written by :class:`CandleRepository`.

    This is the source the offline sweep uses — no broker, no MT5, runs
    anywhere. Filename layout is owned here and nowhere else.
    """

    def __init__(self, root: Path | str = "data/processed") -> None:
        self._root = Path(root)

    def path_for(self, spec: CandleSpec) -> Path:
        return self._root / f"{spec.symbol.lower()}_{spec.interval}.csv"

    def fetch(self, spec: CandleSpec) -> List[Candle]:
        path = self.path_for(spec)
        if not path.exists():
            logger.warning("CsvCandleSource: missing %s", path)
            return []
        out: List[Candle] = []
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                ts = pd.Timestamp(row["time"])
                if ts.tzinfo is None:
                    ts = ts.tz_localize("UTC")
                out.append(
                    Candle(
                        time=ts,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("tick_volume") or 0) or None,
                    )
                )
        return out


class Mt5CandleSource:
    """Pull from a logged-in MT5 terminal.

    ``MetaTrader5`` is imported lazily inside :meth:`fetch` because the package
    is win32-only — importing at module scope would make this whole module
    unimportable on Linux and take the offline sweep down with it.
    """

    def __init__(self, symbol_candidates: Optional[dict[str, Sequence[str]]] = None) -> None:
        self._candidates = symbol_candidates or {}

    def _resolve(self, mt5, symbol: str) -> Optional[str]:
        """Exness appends an ``m`` suffix on some accounts; try both."""
        for name in (*self._candidates.get(symbol, ()), f"{symbol}m", symbol):
            if mt5.symbol_info(name) is not None:
                return name
        return None

    def fetch(self, spec: CandleSpec) -> List[Candle]:
        from .offline_replay import offline_mode

        if offline_mode():
            raise RuntimeError("MT5 market data requires connected mode")
        import MetaTrader5 as mt5  # noqa: PLC0415 — win32-only, must stay lazy

        if mt5.terminal_info() is None and not mt5.initialize():
            logger.error(
                "Mt5CandleSource: mt5.initialize() failed — is the terminal running and logged in? %s",
                mt5.last_error(),
            )
            return []

        tf = {
            "5m": mt5.TIMEFRAME_M5,
            "15m": mt5.TIMEFRAME_M15,
            "1h": mt5.TIMEFRAME_H1,
        }[spec.interval]

        resolved = self._resolve(mt5, spec.symbol)
        if resolved is None:
            logger.error("Mt5CandleSource: no tradable symbol for %s", spec.symbol)
            return []

        rates = mt5.copy_rates_range(resolved, tf, spec.start, spec.end)
        if rates is None or len(rates) == 0:
            logger.error(
                "Mt5CandleSource: empty range for %s %s — %s",
                resolved, spec.interval, mt5.last_error(),
            )
            return []

        return [
            Candle(
                time=pd.Timestamp(int(r["time"]), unit="s", tz="UTC"),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["tick_volume"]),
            )
            for r in rates
        ]


# ── Repository ──────────────────────────────────────────────────────────────

class CandleRepository:
    """Fetch -> validate -> persist, and load back the same way.

    Depends only on the two protocols, so the MT5 pull and the offline sweep run
    through identical validation. That symmetry is the point: data good enough
    to write is data good enough to sweep on.
    """

    def __init__(
        self,
        source: CandleSource,
        validator: Optional[CandleValidator] = None,
        root: Path | str = "data/processed",
    ) -> None:
        self._source = source
        self._validator = validator or CandleValidator()
        self._root = Path(root)

    def _path(self, spec: CandleSpec) -> Path:
        return self._root / f"{spec.symbol.lower()}_{spec.interval}.csv"

    def load(self, spec: CandleSpec) -> tuple[List[Candle], ValidationReport]:
        """Fetch and validate. Candles are returned even when invalid — the
        caller decides whether to proceed; this layer does not silently drop
        data or silently accept it.
        """
        candles = self._source.fetch(spec)
        report = self._validator.validate(candles, spec)
        (logger.info if report.ok else logger.warning)("candles: %s", report.summary())
        return candles, report

    def sync(self, spec: CandleSpec, *, write_on_fail: bool = False) -> ValidationReport:
        """Fetch, validate, and write to disk.

        Refuses to write invalid data by default. A partially-bad CSV is worse
        than a missing one: a missing file stops the sweep loudly, a bad file
        produces plausible numbers that are wrong.
        """
        candles, report = self.load(spec)
        if not report.ok and not write_on_fail:
            logger.error("candles: REFUSING to write %s — %s", self._path(spec), report.summary())
            return report

        path = self._path(spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["time", "open", "high", "low", "close", "tick_volume"])
            for c in candles:
                w.writerow([
                    pd.Timestamp(c.time).isoformat(),
                    c.open, c.high, c.low, c.close, c.volume or 0,
                ])
        logger.info("candles: wrote %s (%d bars)", path, len(candles))
        return report
