import type { AICost, BrokerCost } from './api'

export interface Backtest {
  backtest_id: string
  test_name: string
  asset: string
  start_date: string
  end_date: string
  status: string
  iterations: number
  total_signals: number
  wins: number
  losses: number
  total_pnl: number
  max_drawdown: number
  win_rate: number
  avg_rr: number
  created_at: string
  completed_at?: string
  error_message?: string
}

export interface TradeOutcome {
  trade_reason?: string | null
  trigger_reason?: string | null
  ai_cost?: AICost | null
  broker_cost?: BrokerCost | null
  result?: string
  entry_time?: string
  exit_time?: string
  signal_time?: string
  trade_closed_at?: string
  entry?: number
  exit_price?: number
  profit?: number
  direction?: string
  stop?: number
  initial_stop?: number
  target?: number
  risk_reward?: number
}

export interface Prediction {
  trade_reason?: string | null
  trigger_reason?: string | null
  ai_cost?: AICost | null
  // Canonical field names (backend normalises these before storing)
  direction?: string
  entry?: number
  stop?: number
  target?: number
  pattern?: string
  confidence?: number | string
  status?: string
  guard_rejection?: string
  code_executed?: boolean
  candles_monitored?: number
  triggered_at_candle?: number
  entry_timestamp?: string
}

export interface Iteration {
  ai_cost?: AICost | null
  trade_reason?: string | null
  _id?: string
  iteration: number
  iteration_number?: number
  iteration_timestamp: string
  timestamp?: string
  prediction?: Prediction
  trade_outcome?: TradeOutcome
  execution_result?: Record<string, unknown>
  cycle_data?: Record<string, unknown>
  cycle_id?: string
  status?: string
  error?: string
  monitoring_active?: boolean
}
