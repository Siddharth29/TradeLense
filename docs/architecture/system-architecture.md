# System architecture

TradeLense combines a React frontend, FastAPI services, configurable model decisions,
local strategy execution, MT5 execution and historical replay. The current
configured path is one unified expert plus judge, with conditional typed compilation.
Optional backtests use locally supplied historical candles and the configured decision pipeline.

## Components and responsibilities

| Component | Source | Responsibility |
|---|---|---|
| Frontend | [app](../../apps/frontend/README.md), [backtest types](../../apps/frontend/src/types/backtest.ts) | Agent controls, event timeline and backtest results. Closed result classification uses net P&L, including runners. |
| API / agent lifecycle | [backend](../../apps/backend/README.md), [runner](../../apps/backend/agents/runner.py) | Schedule cycles, obtain validated history, retain setup/position ownership and recover existing positions. |
| Market context | [market structure](../../packages/core/market_structure.py), [asset registry](../../packages/core/asset_registry.py) | Asset-specific contracts, sessions, and time-accurate structure/volatility context. |
| Decisions | [multi_expert](../../packages/core/multi_expert.py), [schemas](../../packages/core/schemas_trade.py) | Unified expert proposals, adjudication, no-trade and trace metadata. |
| Typed strategy | [typed_setup](../../packages/core/typed_setup.py) | Confirmed M15 level references, strict geometry, ordered break/sweep then retest/reclaim, invalidation, expiry and historical-sequence retention. |
| Entry owner | [code sniper service](../../packages/core/services/code_sniper_monitor.py), [executor](../../packages/core/code_sniper_executor.py) | Evaluate an armed strategy on new closed M5 bars and recheck trigger-time eligibility. |
| Broker boundary | [MT5 adapter](../../packages/adapters/mt5_adapter.py), [geometry](../../packages/core/trade_geometry.py), [volume](../../packages/core/volume.py) | Sided executable quote, geometry and lot checks; confirmed fills; exact-ticket operations and ambiguous-send reconciliation. |
| Open position | [monitor service](../../packages/core/services/trade_monitor.py), [trade rules](../../packages/core/trade_monitor.py), [feed](../../packages/core/management_feed.py), [exit families](../../packages/core/exit_policy.py) | One position owner; native exits, partials, acknowledged stops, bar-based structural trail and optional quote targets. |
| Operational state | [ledger](../../packages/core/operational_ledger.py), [exit intents](../../packages/core/operational_execution.py) | Account/deal/position identity, whole-position net outcomes, durable partial intents and recovery. |
| Risk | [account guard](../../packages/core/risk/account_guard.py), [throttle](../../packages/core/risk/trade_throttle.py), [entry policy](../../packages/core/entry_policy.py) | Preserve drawdown baseline; defer entry on unknown risk/history; daily cap/cooldown; remaining session and event horizon. |
| Replay | [backtest runner](../../packages/core/backtest_runner.py), [account/simulator](../../packages/core/backtest_account.py) | Closed-bar chronology, armed/held state, sized net outcomes and per-run isolated ledger. See [flow](backtest-flow.md). |

## Clock and decision contract

MT5 candles retain open timestamps. M5 open 12:00 becomes available at 12:05;
M15 open 12:00 becomes available at 12:15. The runner derives a single `as_of`
from the latest M5 close, passes the configured validated history directly to the
model and filters higher timeframes by their availability. The old cache shortcut
that could reduce history to 50 M5 / 30 M15 bars has been removed.

An active setup is evaluated locally between model decisions. Typed setups use
confirmed level IDs and explicit sequence/invalidation state. Legacy generated
strategies still use the existing sandbox. `prefer` requires exactly one qualifying
proposal across all valid proposals, confidence at least 0.75 and a valid compiler
result; unsupported or competing proposals retain judge adjudication. Re-entry
confidence checks still apply. Trace metadata includes snapshot, compiler/model,
prompt hash and stage/total decision time; it does not establish end-to-end fill latency.

## Execution and position management

Buy entry checks use ask; sell entry checks use bid. The adapter repeats geometry,
TP1 RR and drift qualification on refreshed quotes, floors approved volume to broker
increments and does not raise an approved request to meet the broker minimum.
Confirmed fill price and quantity drive management. Unknown/incomplete receipts must
reconcile; an ambiguous send cannot authorize an immediate duplicate resend.

Native stops remain authoritative. The management feed delivers completed bars and
fresh executable exit quotes to the same owner: long exit uses bid, short exit uses
ask. The feed rejects invalid/stale/non-increasing quote timestamps; its current
age tolerance is -2 to +10 seconds. This is a management-feed contract, not a claim
that entry quote age or post-send slippage is fully solved.

Shadow quotes cannot advance candle confirmations or green TTL extension. In fast
`live` mode, an application target requires an actual executable quote crossing;
bar highs alone do not authorize a quote fill. Typed range reclaims close at the
structural TP1; continuation/sweep families retain partials and runners. Typed
maximum age bounds green and runner extensions. Legacy strategies keep their family.

Recovery matches an exact ticket or stable broker position identifier, retains
unresolved checkpoints, refreshes complete position history and restores broker
quantity/SL while keeping original-stop evidence. Failed position reads do not
mean an empty account. A recovered position without an SL retains monitoring and
raises a critical log; software cannot invent broker protection.

## Persistence and ownership

Connected MT5 operational state is SQLite at `data/operational/ledger.sqlite3`
by default (override `OPERATIONAL_LEDGER_PATH`). Tables: `deals`, `positions`,
`observations`, `operations`, `position_aliases`. Immutable deal IDs deduplicate
fills; all partial/final legs and fees roll into one parent. App ownership follows
opening-deal magic, including a later manual close of an app position.

Connected MT5 throttle and recent outcomes reconcile this ledger independently
of analytics persistence. Legacy brokers without `operational_snapshot` retain
the Mongo throttle fallback. Replay uses an isolated in-memory ledger per runner.
Mongo also stores reporting, configuration and recovery data; SQLite does not
replace every Mongo responsibility.

The deployment assumption is one trading owner per account/terminal. This is not
an atomic multi-host order reservation or portfolio factor-risk allocator. Unsupported
netting reversals fail reconciliation. Unallocated account charges and non-USD R
conversion require explicit treatment; do not silently assign them to a position.

## Configuration checked on September 21

| Setting | Source default / meaning |
|---|---|
| `FAST_POSITION_MODE` | `shadow`; `live` quote targets, `off` bar path. Invalid fast mode logs and retains bar management. |
| `TYPED_SETUP_MODE` | `shadow`; `prefer` conditional judge bypass, `off` no typed compilation. Invalid typed mode raises an error. |
| `TRADING_EVENT_CALENDAR` | Optional JSON path. Valid source, coverage and known-at timestamps required; otherwise static release policy remains. No external provider connected in recorded verification. |
| `PERSIST_ANALYTICS_TO_MONGO` | True by default; analytics policy, not an MT5 ledger enable switch. |
| `CS_DEFAULT_RISK_PCT` / `BT_RISK_PCT` | 1% default risk budget per trade in replay. |
| `RISK_BUDGET_MAX_OVERSHOOT_MULT` | 1.25; gate enabled. Distinct from final approved-volume flooring. |
| `TP2_PARTIAL_FRACTION` | TP2 closes half the remaining volume. TP1 ordinarily closes half the initial volume. Lot constraints affect the final quantities. |
| `CSM_TRADE_EXPIRY_HOURS` | 4; green extension and runner caps are conditionally up to 24h; typed max age can cap sooner. |

See [config.py](../../packages/core/config.py) and capture actual overrides rather
than treating defaults as proof of a running deployment. Session admission requires
more remaining tradable time than the expected horizon (minimum 5 minutes), after
the 5-minute pre-close buffer. An event feed can block new horizon-overlapping
exposure; existing positions keep their established management.

## Public verification profile

Verification covers API startup, the frontend build, expert and judge contracts, backtest engine behavior, broker routing, risk checks, and position management.
