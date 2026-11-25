# Backend

FastAPI API and agent lifecycle. [AgentRunner](agents/runner.py) builds a time-accurate
snapshot, calls the configured decision pipeline and preserves setup/position
management. [Backtest routes](routers/backtest.py) start and inspect historical
runs, including `/backtest/run`, list, iterations, summary, stop and restart.

[System architecture](../../docs/architecture/system-architecture.md) describes
connected MT5 execution, operational ledger, recovery and Mongo responsibilities.
The public verification suite covers API loading, backtest flow, broker routing,
risk services, and position management.
