"""Backtest API router."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

from packages.core.backtest_runner import BacktestRunner
from packages.core.offline_replay import offline_mode
from packages.core.backtest_types import result_type
from packages.core.config import (
    BT_INITIAL_CAPITAL,
    BT_RISK_PCT,
    BT_RISK_SIZING_ENABLED,
)
from packages.db.repositories import backtest as backtest_repo
from packages.core.services.mt5_historical_fetcher import MT5HistoricalFetcher
from packages.core.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/backtest", tags=["backtest"])


class BacktestCreateRequest(BaseModel):
    """Request to create a new backtest."""
    
    test_name: str = Field(..., description="Unique name for the backtest")
    asset: str = Field(default="BTCUSD", description="Asset symbol (e.g., 'BTCUSD', 'GBPUSD', 'EURUSD')")
    start_date: str = Field(default="2024-05-01T00:00:00Z", description="Start date (ISO format)")
    end_date: str = Field(default="2024-10-31T23:59:00Z", description="End date (ISO format)")
    candle_count_5m: int = Field(default=100, description="Number of 5m candles (must be 30, 50, 80, or 100)")
    candle_count_15m: int = Field(default=50, description="Number of 15m candles (must be 30, 50, 80, or 100)")
    use_code_sniper: bool = Field(default=True, description="Use code sniper mode")
    interval_hours: int = Field(default=1, description="Hours between iterations")
    initial_capital: float = Field(
        default=BT_INITIAL_CAPITAL,
        gt=0,
        description="Starting account balance in USD. Trades are sized as balance x risk_pct "
                    "and floored to the broker min_lot, exactly as live does, so this is what "
                    "determines position size and P&L. Compounds across the run.",
    )
    risk_pct: float = Field(
        default=BT_RISK_PCT,
        gt=0,
        lt=1,
        description="Fraction of balance risked per trade (default: the live CS_DEFAULT_RISK_PCT).",
    )
    use_risk_sizing: bool = Field(
        default=BT_RISK_SIZING_ENABLED,
        description="True (default) = size every trade against the account like live, including "
                    "the LP-01 min-lot risk-budget skip. False = fixed lot_size (price-R studies only).",
    )
    lot_size: float = Field(
        default=1.0,
        description="Fixed lot size, used ONLY when use_risk_sizing is False.",
    )
    gpt_config: Dict[str, Any] = Field(default_factory=dict, description="GPT configuration")


class BacktestResponse(BaseModel):
    """Backtest details response."""
    
    backtest_id: str
    test_name: str
    asset: str
    start_date: str
    end_date: str
    status: str
    iterations: int = 0
    total_signals: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    avg_rr: float = 0.0
    created_at: str
    completed_at: Optional[str] = None
    error_message: Optional[str] = None


async def _run_backtest_background(
    test_name: str,
    asset: str,
    start_date: datetime,
    end_date: datetime,
    candle_count_5m: int,
    candle_count_15m: int,
    use_code_sniper: bool,
    interval_hours: int,
    lot_size: float,
    gpt_config: Dict[str, Any],
    backtest_id: str,
    initial_capital: float = BT_INITIAL_CAPITAL,
    risk_pct: float = BT_RISK_PCT,
    use_risk_sizing: bool = BT_RISK_SIZING_ENABLED,
):
    """Run backtest in background task."""
    try:
        logger.info(
            f"[BACKTEST] Starting background backtest '{test_name}'",
            extra={
                "backtest_id": backtest_id,
                "test_name": test_name,
                "asset": asset,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "candle_count_5m": candle_count_5m,
                "candle_count_15m": candle_count_15m,
                "use_code_sniper": use_code_sniper
            }
        )
        
        # Backtests use CSV files exclusively via HistoricalCandleService
        # No need to fetch from MT5 or MongoDB - data is loaded from CSV files
        logger.info(
            f"[BACKTEST] Using CSV file data source for backtest '{test_name}'",
            extra={"backtest_id": backtest_id, "asset": asset}
        )
        
        # Update status to running
        await backtest_repo.update_backtest_status(backtest_id, "running")
        logger.info(f"[BACKTEST] Status updated to 'running' for {backtest_id}")

        # Create runner
        logger.info(f"[BACKTEST] Creating BacktestRunner for {backtest_id}")

        # Load global model config when no model is specified in gpt_config
        if not gpt_config.get("model"):
            try:
                from packages.db.repositories.configs import get_model_config
                model_doc, _ = await get_model_config()
                if model_doc:
                    gpt_config = {**dict(model_doc), **gpt_config}
                    logger.info(f"[BACKTEST] Loaded global model config: model={model_doc.get('model')}")
            except Exception as e:
                logger.warning(f"[BACKTEST] Failed to load global model config: {e}")

        # Persist enriched gpt_config back to the backtest document so we can
        # identify which model was actually used.
        try:
            from packages.db.client import get_client
            from packages.core.config import MONGO_DB_NAME
            _client = get_client()
            await _client[MONGO_DB_NAME]["backtests"].update_one(
                {"_id": ObjectId(backtest_id)},
                {"$set": {"config.gpt_config": gpt_config}},
            )
            logger.info(f"[BACKTEST] Persisted enriched gpt_config (model={gpt_config.get('model')}) to backtest {backtest_id}")
        except Exception as e:
            logger.warning(f"[BACKTEST] Failed to persist enriched gpt_config: {e}")

        # Set AI provider from gpt_config if specified
        _provider = gpt_config.get("provider")
        if _provider:
            from packages.core.gpt_client import set_provider
            set_provider(_provider)

        runner = BacktestRunner(
            asset=asset,
            start_date=start_date,
            end_date=end_date,
            candle_count_5m=candle_count_5m,
            candle_count_15m=candle_count_15m,
            use_code_sniper=use_code_sniper,
            gpt_config=gpt_config,
            lot_size=lot_size,
            initial_capital=initial_capital,
            risk_pct=risk_pct,
            use_risk_sizing=use_risk_sizing,
        )
        logger.info(f"[BACKTEST] Runner created, starting execution for {backtest_id}")

        # Execute backtest
        await runner.execute_backtest(
            test_name=test_name,
            interval_hours=interval_hours,
            backtest_id=backtest_id
        )

        logger.info(
            f"[BACKTEST] Background backtest '{test_name}' completed successfully",
            extra={"backtest_id": backtest_id, "test_name": test_name}
        )

    except Exception as e:
        logger.error(
            f"[BACKTEST] Background backtest '{test_name}' failed: {str(e)}",
            extra={
                "backtest_id": backtest_id,
                "test_name": test_name,
                "error": str(e),
                "error_type": type(e).__name__
            },
            exc_info=True
        )
        
        # Update status to failed
        try:
            await backtest_repo.update_backtest_status(
                backtest_id,
                "failed",
                error_message=str(e)
            )
        except Exception as update_error:
            logger.error(f"[BACKTEST] Failed to update error status: {update_error}")


@router.get("/list", response_model=List[BacktestResponse])
async def list_backtests(
    asset: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 50
) -> List[BacktestResponse]:
    """List all backtests with optional filters.

    Parameters
    ----------
    asset : str, optional
        Filter by asset
    start_date : str, optional
        Filter by start date (ISO format)
    end_date : str, optional
        Filter by end date (ISO format)
    limit : int, optional
        Maximum results (default: 50)

    Returns
    -------
    List[BacktestResponse]
        List of backtests
    """
    # Parse dates if provided
    start_dt = None
    end_dt = None

    if start_date:
        try:
            start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid start_date format")

    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid end_date format")

    # Get backtests
    backtests = await backtest_repo.list_backtests(
        asset=asset,
        start_date=start_dt,
        end_date=end_dt,
        limit=limit
    )

    # Format response
    response = []
    for bt in backtests:
        response.append(BacktestResponse(
            backtest_id=bt["_id"],
            test_name=bt.get("test_name", ""),
            asset=bt.get("asset", ""),
            start_date=bt.get("start_date", datetime.utcnow()).isoformat(),
            end_date=bt.get("end_date", datetime.utcnow()).isoformat(),
            status=bt.get("status", "unknown"),
            iterations=bt.get("iterations", 0),
            total_signals=bt.get("total_signals", 0),
            wins=bt.get("wins", 0),
            losses=bt.get("losses", 0),
            total_pnl=bt.get("total_pnl", 0.0),
            max_drawdown=bt.get("max_drawdown", 0.0),
            win_rate=bt.get("win_rate", 0.0),
            avg_rr=bt.get("avg_rr", 0.0),
            created_at=bt.get("created_at", datetime.utcnow()).isoformat(),
            completed_at=bt.get("completed_at").isoformat() if bt.get("completed_at") else None,
            error_message=bt.get("error_message")
        ))

    return response


@router.post("/run", response_model=Dict[str, Any])
async def run_backtest(
    request: BacktestCreateRequest,
    background_tasks: BackgroundTasks
) -> Dict[str, Any]:
    """Start a new backtest run.

    Parameters
    ----------
    request : BacktestCreateRequest
        Backtest configuration
    background_tasks : BackgroundTasks
        FastAPI background tasks

    Returns
    -------
    Dict[str, Any]
        {"backtest_id": str, "status": str, "test_name": str}
    """
    if offline_mode():
        raise HTTPException(
            status_code=409,
            detail="Backtesting requires connected mode, historical CSVs, and a configured AI provider.",
        )
    try:
        if not request.use_code_sniper:
            raise HTTPException(status_code=400, detail="Backtests require the per-candle trigger mode.")
        # Validate candle counts
        ALLOWED_COUNTS = {30, 50, 80, 100}
        if request.candle_count_5m not in ALLOWED_COUNTS:
            raise HTTPException(
                status_code=400,
                detail=f"candle_count_5m must be one of {sorted(ALLOWED_COUNTS)}, got {request.candle_count_5m}"
            )
        if request.candle_count_15m not in ALLOWED_COUNTS:
            raise HTTPException(
                status_code=400,
                detail=f"candle_count_15m must be one of {sorted(ALLOWED_COUNTS)}, got {request.candle_count_15m}"
            )
        
        # Parse dates
        start_date = datetime.fromisoformat(request.start_date.replace('Z', '+00:00'))
        end_date = datetime.fromisoformat(request.end_date.replace('Z', '+00:00'))

        # Ensure UTC
        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=timezone.utc)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)

        # Validate dates
        if start_date >= end_date:
            raise HTTPException(
                status_code=400,
                detail="start_date must be before end_date"
            )
        
        logger.info(
            f"[BACKTEST] Creating backtest with validated params",
            extra={
                "test_name": request.test_name,
                "asset": request.asset,
                "candle_count_5m": request.candle_count_5m,
                "candle_count_15m": request.candle_count_15m,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat()
            }
        )

        # Create backtest record (will get ID)
        config = {
            "candle_count_5m": request.candle_count_5m,
            "candle_count_15m": request.candle_count_15m,
            "use_code_sniper": request.use_code_sniper,
            "interval_hours": request.interval_hours,
            "lot_size": request.lot_size,
            "initial_capital": request.initial_capital,
            "risk_pct": request.risk_pct,
            "use_risk_sizing": request.use_risk_sizing,
            "gpt_config": request.gpt_config
        }

        backtest_id = await backtest_repo.create_backtest(
            test_name=request.test_name,
            config=config,
            asset=request.asset,
            start_date=start_date,
            end_date=end_date
        )

        # Run backtest in background
        background_tasks.add_task(
            _run_backtest_background,
            test_name=request.test_name,
            asset=request.asset,
            start_date=start_date,
            end_date=end_date,
            candle_count_5m=request.candle_count_5m,
            candle_count_15m=request.candle_count_15m,
            use_code_sniper=request.use_code_sniper,
            interval_hours=request.interval_hours,
            lot_size=request.lot_size,
            initial_capital=request.initial_capital,
            risk_pct=request.risk_pct,
            use_risk_sizing=request.use_risk_sizing,
            gpt_config=request.gpt_config,
            backtest_id=backtest_id
        )

        logger.info(
            f"Backtest '{request.test_name}' queued",
            extra={
                "backtest_id": backtest_id,
                "test_name": request.test_name,
                "asset": request.asset
            }
        )

        return {
            "backtest_id": backtest_id,
            "status": "pending",
            "test_name": request.test_name,
            "message": "Backtest started in background"
        }

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(
            f"Error creating backtest",
            extra={"error": str(e), "error_type": type(e).__name__}
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{backtest_id}/iterations", response_model=List[Dict[str, Any]])
async def get_backtest_iterations(
    backtest_id: str,
    limit: int = 1000  # Increased default for visual playback
) -> List[Dict[str, Any]]:
    """Get all iterations for a backtest.

    Parameters
    ----------
    backtest_id : str
        Backtest ID
    limit : int, optional
        Maximum iterations to return (default: 1000)

    Returns
    -------
    List[Dict[str, Any]]
        List of iteration documents
    """
    iterations = await backtest_repo.get_backtest_iterations(
        backtest_id=backtest_id,
        limit=limit
    )
    
    # Enrich with monitoring window details for visualization
    for iteration in iterations:
        pred = iteration.get("prediction", {})
        if pred.get("status") == "monitoring":
            iteration["monitoring_active"] = True
            iteration["ttl_expiry"] = pred.get("ttl_expiry")
        
        # Extract code execution results
        if iteration.get("execution_result"):
            iteration["code_triggered"] = True

    return iterations




@router.get("/{backtest_id}/summary", response_model=Dict[str, Any])
async def get_backtest_summary(
    backtest_id: str,
    limit: int = 5000,
) -> Dict[str, Any]:
    """Compute comprehensive dashboard metrics from iterations.

    Mirrors the client-side logic in iterations_dashboard.html so that a
    front-end can render KPIs, go-live scorecard, result cards, direction
    breakdown, holding-time stats, equity curve, and insights without doing
    any heavy computation.

    Parameters
    ----------
    backtest_id : str
        Backtest ID
    limit : int
        Max iterations to pull (default 5000)

    Returns
    -------
    Dict[str, Any]
        Full dashboard-ready summary
    """
    backtest = await backtest_repo.get_backtest(backtest_id)
    if not backtest:
        raise HTTPException(status_code=404, detail="Backtest not found")

    raw_iterations = await backtest_repo.get_backtest_iterations(
        backtest_id=backtest_id, limit=limit
    )

    # ── helpers ───────────────────────────────────────────────────────────
    SKIP_RESULTS = {"No Entry", "Filtered", "Flat", ""}
    OPEN_KW = ["open", "kept", "running", "active"]

    # Exact lookup against the canonical vocabulary (packages/core/backtest_types).
    # The previous substring classifier silently mis-binned the labels
    # simulate_trade actually emitted: "Time Exit (Win)"/"Time Exit (Loss)" fell
    # through every branch into "other" — dropped from win/loss counts, win rate,
    # profit factor, expectancy and the scorecard even though their P&L was in
    # total_pnl — while "Time Exit (BE)" matched "BE" and was counted as a
    # break-even. Producers now emit the canonical labels; this reads them.
    _result_type = result_type

    def _parse_dt(v):
        if not v:
            return None
        if isinstance(v, datetime):
            return v
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except Exception:
            return None

    def _hold_ms(entry_t, exit_t):
        d1, d2 = _parse_dt(entry_t), _parse_dt(exit_t)
        if d1 and d2:
            ms = (d2 - d1).total_seconds() * 1000
            return ms if ms > 0 else None
        return None

    def _fmt_dur(ms):
        if not ms:
            return None
        m = round(ms / 60_000)
        if m < 60:
            return f"{m}m"
        h, rm = divmod(m, 60)
        if h < 24:
            return f"{h}h {rm}m"
        d, rh = divmod(h, 24)
        return f"{d}d {rh}h"

    # ── filter closed trades ─────────────────────────────────────────────
    trades = []
    for item in raw_iterations:
        to = item.get("trade_outcome") or {}
        result = (to.get("result") or "").strip()
        if not result or result in SKIP_RESULTS:
            continue
        if any(k in result.lower() for k in OPEN_KW):
            continue
        trades.append(item)

    # ── enrich ───────────────────────────────────────────────────────────
    enriched = []
    for item in trades:
        to = item.get("trade_outcome") or {}
        val = item.get("_validation") or {}
        rtype = _result_type(to.get("result", ""))
        entry = to.get("entry")
        stop = to.get("initial_stop") or to.get("stop")
        target = to.get("target")
        crr = val.get("computed_rr")
        # RR is measured entry -> TP1, matching the live accept gate
        # (CS_MIN_RR_RATIO_TP1). `target` carries the FINAL target (TP2), which
        # the runner runs to but which no gate is measured against — reading RR
        # off it roughly doubles every number and makes the ">= 2R" scorecard
        # criterion pass on setups live scores at half that.
        rr_target = to.get("take_profit_1") or target
        if not crr and entry and stop and rr_target:
            risk = abs(entry - stop)
            if risk > 0:
                crr = round(abs(rr_target - entry) / risk, 4)
        enriched.append({
            "id": item.get("_id"),
            "iteration": item.get("iteration"),
            "direction": (to.get("direction") or "").lower(),
            "result": to.get("result", ""),
            "rtype": rtype,
            "entry": entry,
            "stop": stop,
            "target": target,
            "exit_price": to.get("exit_price"),
            "profit": to.get("profit") or 0.0,
            "rr": crr,
            "entry_time": to.get("entry_time") or to.get("signal_time"),
            "exit_time": to.get("exit_time"),
            "entry_valid": val.get("entry_price_valid"),
            "exit_valid": val.get("exit_price_valid"),
            "sim_match": val.get("result_matches_sim"),
            "overall_valid": val.get("overall_valid"),
        })

    total = len(enriched)
    tp_t = [t for t in enriched if t["rtype"] == "TP"]
    sl_t = [t for t in enriched if t["rtype"] == "SL"]
    be_t = [t for t in enriched if t["rtype"] == "BE"]
    to_t = [t for t in enriched if t["rtype"] == "Timeout"]

    # A trailing stop can close a profitable trade. Match the runner's
    # net-P&L classification and keep exit types for the breakdown below.
    win_t = [t for t in enriched if t["profit"] > 0]
    loss_t = [t for t in enriched if t["profit"] < 0]
    wl = len(win_t) + len(loss_t)
    win_rate = len(win_t) / total if total else 0.0

    total_pnl = sum(t["profit"] for t in enriched)
    gross_profit = sum(t["profit"] for t in win_t)
    gross_loss = abs(sum(t["profit"] for t in loss_t))
    pf = gross_profit / gross_loss if gross_loss > 0 else None

    avg_win = gross_profit / len(win_t) if win_t else 0.0
    avg_loss = gross_loss / len(loss_t) if loss_t else 0.0
    be_profit = sum(t["profit"] for t in be_t)

    rrs = [t["rr"] for t in enriched if t["rr"]]
    avg_rr = sum(rrs) / len(rrs) if rrs else 0.0
    be_rrs = [t["rr"] for t in be_t if t["rr"]]
    avg_be_rr = sum(be_rrs) / len(be_rrs) if be_rrs else 0.0

    # equity curve & max drawdown
    sorted_trades = sorted(enriched, key=lambda t: _parse_dt(t["entry_time"]) or datetime.min)
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    equity_curve = []
    for t in sorted_trades:
        eq += t["profit"]
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
        equity_curve.append({
            "iteration": t["iteration"],
            "cumulative_pnl": round(eq, 2),
            "profit": round(t["profit"], 2),
            "rtype": t["rtype"],
        })

    expectancy = total_pnl / total if total else 0.0
    valid_count = sum(1 for t in enriched if t["overall_valid"])
    mismatch_count = sum(1 for t in enriched if t["sim_match"] is False)

    # ── go-live scorecard ────────────────────────────────────────────────
    criteria = [
        {"label": "Win Rate (wins / closed trades)", "threshold": ">= 40%",
         "value": f"{win_rate * 100:.1f}%" if total else "N/A",
         "pass": win_rate >= 0.40, "weight": 2},
        {"label": "Profit Factor", "threshold": ">= 1.2",
         "value": f"{pf:.3f}" if pf is not None else "N/A",
         "pass": pf is not None and pf >= 1.2, "weight": 2},
        {"label": "Expectancy per trade", "threshold": "> 0",
         "value": f"${expectancy:.2f}",
         "pass": expectancy > 0, "weight": 2},
        {"label": "Avg Setup R:R", "threshold": ">= 2R",
         "value": f"{avg_rr:.3f}R",
         "pass": avg_rr >= 2.0, "weight": 1},
        {"label": "Max Drawdown", "threshold": "< $2000",
         "value": f"${max_dd:.2f}",
         "pass": max_dd < 2000, "weight": 1},
        {"label": "Sample size (W+L decided)", "threshold": ">= 30",
         "value": wl,
         "pass": wl >= 30, "weight": 1},
        {"label": "Data validation rate", "threshold": ">= 75%",
         "value": f"{valid_count / total * 100:.1f}%" if total else "N/A",
         "pass": total > 0 and valid_count / total >= 0.75, "weight": 1},
        {"label": "BE profit (capital saved)", "threshold": "> 0",
         "value": f"${be_profit:.2f}",
         "pass": be_profit >= 0, "weight": 1},
    ]
    score = sum(c["weight"] for c in criteria if c["pass"])
    max_score = sum(c["weight"] for c in criteria)
    score_ratio = score / max_score if max_score else 0
    if score_ratio >= 0.75:
        verdict = "GO"
    elif score_ratio >= 0.5:
        verdict = "CAUTION"
    else:
        verdict = "NOT_READY"

    # ── direction breakdown ──────────────────────────────────────────────
    def _is_long(d):
        return d in ("long", "buy")

    def _is_short(d):
        return d in ("short", "sell")

    direction_breakdown = {
        "long": {
            "tp": sum(1 for t in tp_t if _is_long(t["direction"])),
            "sl": sum(1 for t in sl_t if _is_long(t["direction"])),
            "be": sum(1 for t in be_t if _is_long(t["direction"])),
        },
        "short": {
            "tp": sum(1 for t in tp_t if _is_short(t["direction"])),
            "sl": sum(1 for t in sl_t if _is_short(t["direction"])),
            "be": sum(1 for t in be_t if _is_short(t["direction"])),
        },
    }
    long_tp, long_sl = direction_breakdown["long"]["tp"], direction_breakdown["long"]["sl"]
    short_tp, short_sl = direction_breakdown["short"]["tp"], direction_breakdown["short"]["sl"]
    long_wr = long_tp / (long_tp + long_sl) if (long_tp + long_sl) else None
    short_wr = short_tp / (short_tp + short_sl) if (short_tp + short_sl) else None

    # ── holding time stats ───────────────────────────────────────────────
    hold_times = [_hold_ms(t["entry_time"], t["exit_time"]) for t in enriched]
    hold_times = [h for h in hold_times if h]
    avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0
    min_hold = min(hold_times) if hold_times else 0
    max_hold = max(hold_times) if hold_times else 0

    tp_holds = [_hold_ms(t["entry_time"], t["exit_time"]) for t in tp_t]
    tp_holds = [h for h in tp_holds if h]
    sl_holds = [_hold_ms(t["entry_time"], t["exit_time"]) for t in sl_t]
    sl_holds = [h for h in sl_holds if h]
    be_holds = [_hold_ms(t["entry_time"], t["exit_time"]) for t in be_t]
    be_holds = [h for h in be_holds if h]

    # ── RR histogram buckets ─────────────────────────────────────────────
    rr_buckets = {"<1": 0, "1-2": 0, "2-3": 0, "3-4": 0, "4-5": 0, "5-6": 0, ">6": 0}
    for t in enriched:
        v = t["rr"]
        if not v:
            continue
        if v < 1:
            rr_buckets["<1"] += 1
        elif v < 2:
            rr_buckets["1-2"] += 1
        elif v < 3:
            rr_buckets["2-3"] += 1
        elif v < 4:
            rr_buckets["3-4"] += 1
        elif v < 5:
            rr_buckets["4-5"] += 1
        elif v < 6:
            rr_buckets["5-6"] += 1
        else:
            rr_buckets[">6"] += 1

    be_rr_buckets = {"<1": 0, "1-2": 0, "2-3": 0, "3-4": 0, "4-5": 0, "5-6": 0, ">6": 0}
    for t in be_t:
        v = t["rr"]
        if not v:
            continue
        if v < 1:
            be_rr_buckets["<1"] += 1
        elif v < 2:
            be_rr_buckets["1-2"] += 1
        elif v < 3:
            be_rr_buckets["2-3"] += 1
        elif v < 4:
            be_rr_buckets["3-4"] += 1
        elif v < 5:
            be_rr_buckets["4-5"] += 1
        elif v < 6:
            be_rr_buckets["5-6"] += 1
        else:
            be_rr_buckets[">6"] += 1

    # ── sub-metrics per result type ──────────────────────────────────────
    tp_rrs = [t["rr"] for t in tp_t if t["rr"]]
    sl_rrs = [t["rr"] for t in sl_t if t["rr"]]
    tp_pnl = sum(t["profit"] for t in tp_t)
    sl_pnl = sum(t["profit"] for t in sl_t)
    to_pnl = sum(t["profit"] for t in to_t)

    # ── assemble response ────────────────────────────────────────────────
    return {
        "backtest_id": backtest["_id"],
        "test_name": backtest.get("test_name"),
        "asset": backtest.get("asset"),
        "status": backtest.get("status"),
        "created_at": backtest.get("created_at", "").isoformat() if isinstance(backtest.get("created_at"), datetime) else backtest.get("created_at"),

        # ── KPIs ─────────────────────────────────────────────────────────
        "kpis": {
            "total_closed": total,
            "win_count": len(win_t),
            "loss_count": len(loss_t),
            "win_rate": round(win_rate, 4),
            "total_pnl": round(total_pnl, 2),
            "profit_factor": round(pf, 3) if pf is not None else None,
            "expectancy": round(expectancy, 2),
            "avg_rr": round(avg_rr, 3),
            "max_drawdown": round(max_dd, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "validation_rate": round(valid_count / total, 4) if total else 0,
            "valid_count": valid_count,
            "mismatch_count": mismatch_count,
        },

        # ── result breakdown ─────────────────────────────────────────────
        "results": {
            "tp": {
                "count": len(tp_t),
                "total_pnl": round(tp_pnl, 2),
                "avg_profit": round(avg_win, 2),
                "avg_rr": round(sum(tp_rrs) / len(tp_rrs), 3) if tp_rrs else 0,
            },
            "sl": {
                "count": len(sl_t),
                "total_pnl": round(sl_pnl, 2),
                "avg_loss": round(avg_loss, 2),
                "avg_rr": round(sum(sl_rrs) / len(sl_rrs), 3) if sl_rrs else 0,
            },
            "be": {
                "count": len(be_t),
                "total_pnl": round(be_profit, 2),
                "avg_profit": round(be_profit / len(be_t), 2) if be_t else 0,
                "avg_rr": round(avg_be_rr, 3),
            },
            "timeout": {
                "count": len(to_t),
                "total_pnl": round(to_pnl, 2),
                "avg_pnl": round(to_pnl / len(to_t), 2) if to_t else 0,
                "capital_saved": sum(1 for t in to_t if t["profit"] >= 0),
            },
        },

        # ── go-live scorecard ────────────────────────────────────────────
        "scorecard": {
            "verdict": verdict,
            "score": score,
            "max_score": max_score,
            "criteria": criteria,
        },

        # ── direction breakdown ──────────────────────────────────────────
        "direction_breakdown": direction_breakdown,
        "long_win_rate": round(long_wr, 4) if long_wr is not None else None,
        "short_win_rate": round(short_wr, 4) if short_wr is not None else None,

        # ── holding times (ms) ───────────────────────────────────────────
        "holding_times": {
            "avg_ms": round(avg_hold),
            "min_ms": round(min_hold),
            "max_ms": round(max_hold),
            "avg_formatted": _fmt_dur(avg_hold),
            "min_formatted": _fmt_dur(min_hold),
            "max_formatted": _fmt_dur(max_hold),
            "tp_avg_formatted": _fmt_dur(sum(tp_holds) / len(tp_holds) if tp_holds else 0),
            "sl_avg_formatted": _fmt_dur(sum(sl_holds) / len(sl_holds) if sl_holds else 0),
            "be_avg_formatted": _fmt_dur(sum(be_holds) / len(be_holds) if be_holds else 0),
            "trade_count": len(hold_times),
        },

        # ── chart data ───────────────────────────────────────────────────
        "equity_curve": equity_curve,
        "rr_histogram": rr_buckets,
        "be_rr_histogram": be_rr_buckets,
    }


@router.get("/{backtest_id}", response_model=BacktestResponse)
async def get_backtest(backtest_id: str) -> BacktestResponse:
    """Get backtest details by ID.

    Parameters
    ----------
    backtest_id : str
        Backtest ID

    Returns
    -------
    BacktestResponse
        Backtest details
    """
    backtest = await backtest_repo.get_backtest(backtest_id)

    if not backtest:
        raise HTTPException(status_code=404, detail="Backtest not found")

    # Format response
    return BacktestResponse(
        backtest_id=backtest["_id"],
        test_name=backtest.get("test_name", ""),
        asset=backtest.get("asset", ""),
        start_date=backtest.get("start_date", datetime.utcnow()).isoformat(),
        end_date=backtest.get("end_date", datetime.utcnow()).isoformat(),
        status=backtest.get("status", "unknown"),
        iterations=backtest.get("iterations", 0),
        total_signals=backtest.get("total_signals", 0),
        wins=backtest.get("wins", 0),
        losses=backtest.get("losses", 0),
        total_pnl=backtest.get("total_pnl", 0.0),
        max_drawdown=backtest.get("max_drawdown", 0.0),
        win_rate=backtest.get("win_rate", 0.0),
        avg_rr=backtest.get("avg_rr", 0.0),
        created_at=backtest.get("created_at", datetime.utcnow()).isoformat(),
        completed_at=backtest.get("completed_at").isoformat() if backtest.get("completed_at") else None,
        error_message=backtest.get("error_message")
    )




@router.post("/{backtest_id}/stop", response_model=Dict[str, Any])
async def stop_backtest(backtest_id: str) -> Dict[str, Any]:
    """Stop a running backtest.

    Parameters
    ----------
    backtest_id : str
        Backtest ID

    Returns
    -------
    Dict[str, Any]
        {"success": bool, "message": str, "status": str}
    """
    try:
        # Get current backtest
        backtest = await backtest_repo.get_backtest(backtest_id)
        if not backtest:
            raise HTTPException(status_code=404, detail="Backtest not found")
        
        current_status = backtest.get("status", "unknown")
        
        # Only allow stopping if running or pending
        if current_status not in ["running", "pending"]:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot stop backtest with status '{current_status}'"
            )
        
        # Update status to "stopped" — the BacktestRunner polls this status
        # every iteration and will break out of its loop when it sees a
        # non-running/pending status.
        success = await backtest_repo.update_backtest_status(
            backtest_id,
            "stopped",
            error_message="Stopped by user"
        )

        if not success:
            raise HTTPException(status_code=500, detail="Failed to update status")

        logger.info(
            f"[BACKTEST] Backtest {backtest_id} stop requested by user",
            extra={"backtest_id": backtest_id, "previous_status": current_status}
        )

        return {
            "success": True,
            "message": "Backtest stop requested — will halt at next iteration",
            "status": "stopped",
            "backtest_id": backtest_id
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to stop backtest {backtest_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{backtest_id}/restart", response_model=Dict[str, Any])
async def restart_backtest(
    backtest_id: str,
    background_tasks: BackgroundTasks
) -> Dict[str, Any]:
    """Restart a failed or stopped backtest.

    Parameters
    ----------
    backtest_id : str
        Backtest ID
    background_tasks : BackgroundTasks
        FastAPI background tasks

    Returns
    -------
    Dict[str, Any]
        {"success": bool, "message": str, "status": str}
    """
    if offline_mode():
        raise HTTPException(
            status_code=409,
            detail="Backtesting requires connected mode, historical CSVs, and a configured AI provider.",
        )
    try:
        # Get backtest details
        backtest = await backtest_repo.get_backtest(backtest_id)
        if not backtest:
            raise HTTPException(status_code=404, detail="Backtest not found")
        
        current_status = backtest.get("status", "unknown")
        
        # Only allow restarting if failed or completed
        if current_status not in ["failed", "completed"]:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot restart backtest with status '{current_status}'. Only 'failed' or 'completed' backtests can be restarted."
            )
        
        # Reset status to pending
        await backtest_repo.update_backtest_status(backtest_id, "pending")
        
        # Extract config
        config = backtest.get("config", {})
        test_name = backtest.get("test_name", "")
        asset = backtest.get("asset", "NIFTY")
        start_date = backtest.get("start_date")
        end_date = backtest.get("end_date")
        
        # Ensure timezone-aware
        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=timezone.utc)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        
        # Run backtest in background.
        # `lot_size` was missing here, so every restart raised TypeError inside
        # the background task (a required positional argument) and the run died
        # silently with the document stuck on "pending". The account fields are
        # passed for the same reason: a restart must reproduce the original run,
        # not fall back to defaults.
        background_tasks.add_task(
            _run_backtest_background,
            test_name=test_name,
            asset=asset,
            start_date=start_date,
            end_date=end_date,
            candle_count_5m=config.get("candle_count_5m", 100),
            candle_count_15m=config.get("candle_count_15m", 50),
            use_code_sniper=config.get("use_code_sniper", True),
            interval_hours=config.get("interval_hours", 1),
            lot_size=config.get("lot_size", 1.0),
            initial_capital=config.get("initial_capital", BT_INITIAL_CAPITAL),
            risk_pct=config.get("risk_pct", BT_RISK_PCT),
            use_risk_sizing=config.get("use_risk_sizing", BT_RISK_SIZING_ENABLED),
            gpt_config=config.get("gpt_config", {}),
            backtest_id=backtest_id
        )
        
        logger.info(
            f"[BACKTEST] Backtest {backtest_id} restarted",
            extra={"backtest_id": backtest_id, "previous_status": current_status}
        )
        
        return {
            "success": True,
            "message": f"Backtest restarted",
            "status": "pending",
            "backtest_id": backtest_id
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to restart backtest {backtest_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/fetch-historical-data", response_model=Dict[str, Any])
async def fetch_historical_data(
    asset: str,
    start_date: str,
    end_date: str,
    intervals: List[str] = None,
    background_tasks: BackgroundTasks = None
) -> Dict[str, Any]:
    """Fetch historical data from MT5 and store in MongoDB.
    
    Parameters
    ----------
    asset : str
        Trading symbol (e.g., 'BTCUSD', 'NIFTY')
    start_date : str
        Start date in ISO format (e.g., '2024-05-01T00:00:00Z')
    end_date : str
        End date in ISO format (e.g., '2024-10-31T23:59:59Z')
    intervals : List[str], optional
        List of intervals to fetch (default: ['5m', '15m'])
    background_tasks : BackgroundTasks
        FastAPI background tasks
    
    Returns
    -------
    Dict[str, Any]
        {"success": bool, "message": str, "candles_count": dict}
    """
    try:
        # Parse dates
        start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
        end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
        
        # Ensure UTC
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        
        # Validate dates
        if start_dt >= end_dt:
            raise HTTPException(
                status_code=400,
                detail="start_date must be before end_date"
            )
        
        if end_dt > datetime.now(timezone.utc):
            raise HTTPException(
                status_code=400,
                detail="end_date cannot be in the future"
            )
        
        # Set default intervals
        if intervals is None:
            intervals = ["5m", "15m"]
        
        logger.info(
            f"[FETCH-HISTORICAL] Fetching data for {asset} from {start_dt} to {end_dt}",
            extra={
                "asset": asset,
                "start_date": start_dt.isoformat(),
                "end_date": end_dt.isoformat(),
                "intervals": intervals
            }
        )
        
        # Create fetcher and fetch data
        fetcher = MT5HistoricalFetcher()
        
        try:
            results = await fetcher.fetch_and_store_historical_data(
                asset=asset,
                start_date=start_dt,
                end_date=end_dt,
                intervals=intervals
            )
            
            logger.info(
                f"[FETCH-HISTORICAL] Successfully fetched data for {asset}",
                extra={"asset": asset, "results": results}
            )
            
            return {
                "success": True,
                "message": f"Successfully fetched {sum(results.values())} candles for {asset}",
                "candles_count": results
            }
            
        except Exception as e:
            logger.error(
                f"[FETCH-HISTORICAL] Failed to fetch data for {asset}: {e}",
                extra={"asset": asset, "error": str(e)},
                exc_info=True
            )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to fetch historical data: {str(e)}"
            )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[FETCH-HISTORICAL] Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{backtest_id}", response_model=Dict[str, Any])
async def delete_backtest(backtest_id: str) -> Dict[str, Any]:
    """Delete a backtest and all its iterations.

    Parameters
    ----------
    backtest_id : str
        Backtest ID

    Returns
    -------
    Dict[str, Any]
        {"success": bool, "message": str}
    """
    success = await backtest_repo.delete_backtest(backtest_id)

    if not success:
        raise HTTPException(status_code=404, detail="Backtest not found")

    return {
        "success": True,
        "message": f"Backtest {backtest_id} deleted successfully"
    }


@router.delete("/candles/{asset}", response_model=Dict[str, Any])
async def delete_all_candles(
    asset: str,
    interval: Optional[str] = None
) -> Dict[str, Any]:
    """Delete all candles for an asset.

    Parameters
    ----------
    asset : str
        Asset symbol
    interval : str, optional
        Interval to delete ("5m", "15m", or None for both)

    Returns
    -------
    Dict[str, Any]
        {"success": bool, "message": str, "deleted_count": dict}
    """
    from packages.db.repositories import candles as candle_repo
    
    deleted_counts = {}
    
    if interval is None or interval == "5m":
        count_5m = await candle_repo.delete_all_5m(asset)
        deleted_counts["5m"] = count_5m
    
    if interval is None or interval == "15m":
        count_15m = await candle_repo.delete_all_15m(asset)
        deleted_counts["15m"] = count_15m
    
    total_deleted = sum(deleted_counts.values())
    
    logger.info(
        f"[DELETE-CANDLES] Deleted {total_deleted} candles for {asset}",
        extra={"asset": asset, "interval": interval, "deleted_counts": deleted_counts}
    )
    
    return {
        "success": True,
        "message": f"Deleted {total_deleted} candles for {asset}",
        "deleted_count": deleted_counts
    }


@router.delete("/candles/{asset}/range", response_model=Dict[str, Any])
async def delete_candles_range(
    asset: str,
    start_date: str,
    end_date: str,
    interval: Optional[str] = None
) -> Dict[str, Any]:
    """Delete candles for an asset within a date range.

    Parameters
    ----------
    asset : str
        Asset symbol
    start_date : str
        Start date (ISO format)
    end_date : str
        End date (ISO format)
    interval : str, optional
        Interval to delete ("5m", "15m", or None for both)

    Returns
    -------
    Dict[str, Any]
        {"success": bool, "message": str, "deleted_count": dict}
    """
    from packages.db.repositories import candles as candle_repo
    
    # Parse dates
    start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
    end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
    
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    
    deleted_counts = {}
    
    if interval is None or interval == "5m":
        count_5m = await candle_repo.delete_many_5m(
            asset=asset,
            start=start_dt,
            end=end_dt
        )
        deleted_counts["5m"] = count_5m
    
    if interval is None or interval == "15m":
        count_15m = await candle_repo.delete_many_15m(
            asset=asset,
            start=start_dt,
            end=end_dt
        )
        deleted_counts["15m"] = count_15m
    
    total_deleted = sum(deleted_counts.values())
    
    logger.info(
        f"[DELETE-CANDLES-RANGE] Deleted {total_deleted} candles for {asset} "
        f"from {start_dt} to {end_dt}",
        extra={
            "asset": asset,
            "start_date": start_dt.isoformat(),
            "end_date": end_dt.isoformat(),
            "interval": interval,
            "deleted_counts": deleted_counts
        }
    )
    
    return {
        "success": True,
        "message": f"Deleted {total_deleted} candles for {asset} in date range",
        "deleted_count": deleted_counts
    }


@router.get("/candles/stats/{asset}", response_model=Dict[str, Any])
async def get_candle_stats(
    asset: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
) -> Dict[str, Any]:
    """Get candle statistics for an asset.

    Parameters
    ----------
    asset : str
        Asset symbol
    start_date : str, optional
        Start date (ISO format)
    end_date : str, optional
        End date (ISO format)

    Returns
    -------
    Dict[str, Any]
        Statistics with counts and date ranges for 5m and 15m candles
    """
    from packages.db.repositories import candles as candle_repo
    
    # Parse dates if provided
    start_dt = None
    end_dt = None
    
    if start_date:
        start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
    
    if end_date:
        end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
    
    # Get counts
    count_5m = await candle_repo.count_5m(asset=asset, start=start_dt, end=end_dt)
    count_15m = await candle_repo.count_15m(asset=asset, start=start_dt, end=end_dt)
    
    # Get date ranges if candles exist
    date_range_5m = None
    date_range_15m = None
    
    if count_5m > 0:
        candles_5m = await candle_repo.get_many_5m(
            asset=asset,
            start=start_dt,
            end=end_dt,
            limit=1
        )
        if candles_5m:
            first_5m = candles_5m[0].get("time")
            last_candles_5m = await candle_repo.get_many_5m(
                asset=asset,
                start=start_dt,
                end=end_dt,
                limit=count_5m
            )
            if last_candles_5m:
                last_5m = last_candles_5m[-1].get("time")
                date_range_5m = {
                    "min": first_5m.isoformat() if isinstance(first_5m, datetime) else str(first_5m),
                    "max": last_5m.isoformat() if isinstance(last_5m, datetime) else str(last_5m)
                }
    
    if count_15m > 0:
        candles_15m = await candle_repo.get_many_15m(
            asset=asset,
            start=start_dt,
            end=end_dt,
            limit=1
        )
        if candles_15m:
            first_15m = candles_15m[0].get("time")
            last_candles_15m = await candle_repo.get_many_15m(
                asset=asset,
                start=start_dt,
                end=end_dt,
                limit=count_15m
            )
            if last_candles_15m:
                last_15m = last_candles_15m[-1].get("time")
                date_range_15m = {
                    "min": first_15m.isoformat() if isinstance(first_15m, datetime) else str(first_15m),
                    "max": last_15m.isoformat() if isinstance(last_15m, datetime) else str(last_15m)
                }
    
    return {
        "asset": asset,
        "5m": {
            "count": count_5m,
            "date_range": date_range_5m
        },
        "15m": {
            "count": count_15m,
            "date_range": date_range_15m
        }
    }


@router.get("/candles/count/{asset}", response_model=Dict[str, Any])
async def count_candles(
    asset: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    interval: str = "5m"
) -> Dict[str, Any]:
    """Count candles for an asset.

    Parameters
    ----------
    asset : str
        Asset symbol
    start_date : str, optional
        Start date (ISO format)
    end_date : str, optional
        End date (ISO format)
    interval : str
        Interval ("5m" or "15m")

    Returns
    -------
    Dict[str, Any]
        {"count": int, "interval": str}
    """
    from packages.db.repositories import candles as candle_repo
    
    if interval not in ("5m", "15m"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid interval: {interval}. Must be '5m' or '15m'"
        )
    
    # Parse dates if provided
    start_dt = None
    end_dt = None
    
    if start_date:
        start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
    
    if end_date:
        end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
    
    # Get count
    if interval == "5m":
        count = await candle_repo.count_5m(asset=asset, start=start_dt, end=end_dt)
    else:
        count = await candle_repo.count_15m(asset=asset, start=start_dt, end=end_dt)
    
    return {
        "count": count,
        "interval": interval,
        "asset": asset
    }
