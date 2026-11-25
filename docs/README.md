# TradeLense Documentation

TradeLense combines AI-assisted market analysis with deterministic validation,
chronological no-lookahead backtesting, disciplined risk control, position
management, and end-to-end audit evidence.

## Start here

- [Screenshot gallery](screenshots/README.md) with application views and the trading workflow.

- [Quick flow](architecture/quick-flow.md): the complete trade lifecycle at a glance.
- [System architecture](architecture/system-architecture.md): frontend, API, model,
  strategy, execution, persistence, and reporting boundaries.
- [Backtest flow](architecture/backtest-flow.md): time-accurate replay from market
  snapshot through entry, trailing, exit, and result aggregation.
- [Historical candle setup](../data/processed/README.md): local files and CSV schema.

## Core engineering areas

| Area | Demonstrated capability |
|---|---|
| Expert and judge | Structured market proposals, independent review, and schema-bound decisions |
| Risk engine | Position sizing, stop/target geometry, drawdown gates, exposure limits, and cooldowns |
| Backtesting | Chronological no-lookahead snapshots, deterministic state transitions, and repeatable results |
| Position management | Partial exits, protective stop movement, trailing, runner handling, and time-based closure |
| Audit trail | Decision-to-outcome traceability across proposals, validations, entries, management, and exits |
| User interface | React dashboard for agents, decisions, backtests, configurations, logs, and analysis |

The [repository README](../README.md) has the offline setup, configuration
locations, and replay verification command.

