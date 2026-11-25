# Core Package

> **Status:** living · execution map updated 2026-09-21

Current core paths: [backtest runner](backtest_runner.py), [closed-bar feed](backtest_bar_feed.py),
[account/simulator](backtest_account.py), [typed setups](typed_setup.py),
[entry policy](entry_policy.py), [exit families](exit_policy.py),
[management feed](management_feed.py), [operational ledger](operational_ledger.py)
and [durable partials](operational_execution.py). See the
[system architecture](../../docs/architecture/system-architecture.md) for runtime behavior.
The Assistant-ID helpers below are legacy-compatible entry points, not a description
of the complete configured expert/judge pipeline.

Shared core utilities.

## Assistant ID

Core helpers distinguish between an `agent_id` and an `assistant_id`.
The former is the local identifier for a strategy, while the latter
references the OpenAI Assistant used to produce trade decisions.

Functions such as [`sniper_agent.call_sniper`](sniper_agent.py) and the
backend's [`AgentRunner`](../../apps/backend/agents/runner.py) default to the value of
[`ASSISTANT_ID`](config.py) when no `assistant_id` is provided.
Override the assistant by passing an explicit identifier or by editing the
constant in `config.py`.

```python
from packages.core.sniper_agent import call_sniper

# uses config.ASSISTANT_ID
await call_sniper(c15, c5, ts)

# override the assistant
await call_sniper(c15, c5, ts, assistant_id="asst_ABC123")
```

## Risk utilities

The `packages.core.risk` module contains helpers for position sizing. The
``calc_risk_reward`` function returns the reward‑to‑risk ratio given an entry,
stop and take‑profit.  :class:`RiskService` can enforce a minimum ratio by
providing ``min_reward_to_risk`` when instantiated. When ``take_profit`` is
supplied to :meth:`RiskService.calc_quantity` and the ratio falls below this
threshold a :class:`ValueError` is raised.
