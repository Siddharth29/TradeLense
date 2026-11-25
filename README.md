# TradeLense

## AI Trading Agents, Risk Management & Audit

TradeLense is an AI-assisted trading platform shaped by **2.5 years of building, testing, and refining**. Parallel MT5 agents propose trades through an expert–judge pipeline, apply deterministic risk checks before entry, and track positions from fill through trailing stops, exits, and reconciliation. Optional historical replay helps verify that workflow.

## Why this project exists

A price-action thesis needs a precise entry condition. TradeLense turns observations such as swing structure, momentum, and nearby levels into a proposal the system can review, monitor, and audit. The goal is to make every trade explainable from the initial market evidence through the entry decision, risk checks, stop moves, and final outcome.


```text
Closed candles → expert proposal → judge review → sandboxed trigger → risk checks
  Accepted → fill → partial exits and trailing SL → reconciliation → audit
  Rejected → decision and reason recorded in the audit trail
```

- **Parallel agents.** In connected mode, run multiple MT5 agents across your configured instruments. The supervisor schedules a background task for each agent, with per-agent settings and shared account risk checks.
- **Optional backtesting.** Replay advances through closed candles in timestamp order, exercising entry, risk, trailing, exits, and audit handling on historical data.
- **Trailing stop-loss and partial exits.** The position manager progressively tightens the stop as a trade moves in its favor. Partial exits can book profit and leave a runner under an ATR-based trail. The goal is to protect gains and preserve positive net P&L when conditions allow. A stop level alone does not guarantee a positive net result after execution and costs.
- **Audit and reconciliation.** Review proposals, judge verdicts, rejected entries, fills, stop moves, partial exits, and final net P&L. Connected MT5 deal records reconcile fills and exits with the operational ledger.

## The decision engine

1. **Expert reads price action.** Using completed candles and higher-timeframe context, the expert proposes a direction, entry, stop, and target. Its rationale explains what supports the setup and what would invalidate it.
2. **Judge reviews and arms the setup.** The judge can return no trade. When it accepts a code-based setup, it returns an actual Python entry-monitor function, `run(candles_5, now)`, and a time limit for the setup.
3. **The sandbox checks each closed five-minute candle.** The monitor passes the generated function and closed-candle history to a restricted Python sandbox. A hold result normally keeps the setup armed. A trigger or expiry moves the workflow forward.
4. **Execution applies independent risk checks.** Before placing an order, TradeLense rechecks reward-to-risk, sizing, exposure, margin, and setup expiry. The audit trail connects the proposal, judge verdict, trigger, and execution outcome.


## Risk and session controls

| Control | Default and configuration |
| --- | --- |
| Risk per trade | **1%** by default. Use `CS_LIVE_RISK_PCT` for live agents and `BT_RISK_PCT` for backtests. |
| Daily halt | **10% account drawdown** from the daily equity baseline blocks new entries. Open positions retain protective management. Set `ACCOUNT_MAX_DAILY_DD_PCT` and `ACCOUNT_DAILY_DD_GUARD_ENABLED`. |
| Weekly market halt | Agents pause during the instrument's scheduled weekend closure and resume when its session reopens. Crypto follows its own session rules. The account-loss halt uses a daily baseline. |
| ATR stop guidance | **1× hourly ATR over 14 periods** by default, subject to instrument distance limits. Set `ATR_STOP_MULT`, `ATR_STOP_PERIOD`, or `ATR_STOP_MULT_BY_ASSET`. |
| Trailing stop | `DEFAULT_TRAIL_STEPS` sets the profit milestones and stop levels in units of initial risk. `RUNNER_TRAIL_ATR_MULT` sets the ATR distance for the runner. |

See [risk configuration](packages/core/config.py), [market sessions](packages/core/session.py), and the [system architecture](docs/architecture/system-architecture.md).

## Quick start

Install **Python 3.12** and **Node.js 20**. On Windows, run these commands from the repository root.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
.\start_dev.bat
```

Open [TradeLense](http://127.0.0.1:5173/) to explore the dashboard. The default local launch keeps broker and model calls disabled.

### Connected mode

Copy [`.env.example`](.env.example) to `.env`, set `TRADELENS_OFFLINE=false`, and configure `MONGO_URI` for connected storage. Start the API with `python -m uvicorn apps.backend.api:app --port 8000`. In a second terminal, run `npm ci` and `npm run dev` from `apps/frontend`.

Users can configure [expert and judge prompts](packages/core/prompt_builders.py), AI provider credentials in `.env`, and demo MT5 credentials under **Agents → Connect** with the Windows MT5 terminal installed.

### Optional backtesting

In connected mode, configure an AI provider and supply [historical candle CSVs](data/processed/README.md). Open [Backtest Setup](http://127.0.0.1:5173/backtests/create) and select a symbol and date range covered by your files.

Use the run to verify engineering behavior: candle timing, risk gates, position state, trailing stops, and audit reconciliation. Fresh AI decisions can vary across runs, so replay results are evidence about the system under the recorded inputs and configuration, not a source of truth for profitability. Keep the data window, model, settings, and trade count with each result.

## Screenshots and verification

[Dashboard](docs/screenshots/README.md#dashboard) ·
[MT5 agents](docs/screenshots/README.md#mt5-agents) ·
[Model configuration](docs/screenshots/README.md#model-configuration) ·
[Backtest setup](docs/screenshots/README.md#backtest-setup) ·
[Trading analysis](docs/screenshots/README.md#trading-analysis) ·
[Trading workflow](docs/screenshots/README.md#trading-workflow)

Run `python -m pytest tests/test_offline_replay.py -q` to check offline access and verify that connected actions are blocked. Run `npm run build` from `apps/frontend` for the production frontend build.
