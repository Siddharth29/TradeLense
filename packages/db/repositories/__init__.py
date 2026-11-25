"""Database repository utilities."""

from .write import (
    insert_trades,
    insert_orders,
    insert_fills,
    insert_signals,
    insert_logs,
    insert_daily_metrics,
)
from .read import (
    get_trades,
    get_orders,
    get_fills,
    list_signals,
    list_logs,
    get_candles,
)
from .aggregations import aggregate_agent_metrics
from .wallets import insert_wallet_snapshot, get_last_wallet_snapshot
from .agent_runs import insert_agent_run, get_agent_runs
from .active_trades import (
    store_active_trade,
    get_active_trades_for_agent,
    get_active_trade_by_symbol,
    update_active_trade,
    clear_active_trade,
    clear_all_active_trades_for_agent,
    get_all_active_trades,
    cleanup_old_trades,
)

__all__ = [
    "insert_trades",
    "insert_orders",
    "insert_fills",
    "insert_signals",
    "insert_logs",
    "insert_daily_metrics",
    "insert_wallet_snapshot",
    "get_trades",
    "get_orders",
    "get_fills",
    "list_signals",
    "list_logs",
    "get_candles",
    "get_last_wallet_snapshot",
    "aggregate_agent_metrics",
    "insert_agent_run",
    "get_agent_runs",
    "store_active_trade",
    "get_active_trades_for_agent",
    "get_active_trade_by_symbol",
    "update_active_trade",
    "clear_active_trade",
    "clear_all_active_trades_for_agent",
    "get_all_active_trades",
    "cleanup_old_trades",
]
