import { z } from 'zod'

export interface AgentStatus {
  running: boolean;
  error?: string;
  cancelled?: boolean;
  done?: boolean;
  run_interval?: string;
  risk_percent?: number;
}

export interface Metrics {
  runs: number;
  decisions: number;
  orders: number;
  wins: number;
  losses: number;
  pnl: number;
  realized_pnl?: number;
  unrealized_pnl?: number;
  open_trades?: number;
  closed_trades?: number;
  budget_skips: number;
  latency_p50?: number | null;
  latency_p95?: number | null;
  drift_p50?: number | null;
  drift_p95?: number | null;
  hit_rate?: number | null;
  last_run: string | null;
  eta: string | null;
  summary: string;
  running: boolean;
  usage: number | null;
  agent_id?: string | null;
  run_interval?: string | null;
  risk_percent?: number | null;
}

export interface BookedPnl {
  gross: number;
  charges: number;
  net: number;
}

export interface SystemMetrics {
  account_pnl?: {
    currency: string;
    all_time: BookedPnl;
    today: BookedPnl;
    floating: number;
    daily_curve: { time: string; pnl: number }[];
  } | null;
  equity_curve: { time: string; equity: number }[];
  daily_pnl: { time: string; pnl: number }[];
  win_rate: number;
  latency_p50?: number | null;
  latency_p95?: number | null;
  token_spend?: number;
}

export type HealthStatus = Record<string, { broker: boolean }>;

export interface HealthResponse {
  time: string;
  status: HealthStatus;
}

export type Flags = Record<string, boolean>;

export interface Readiness {
  ready: boolean;
  blockers: Record<string, string>;
  time?: string;
}

export interface JournalRow {
  timestamp: string;
  type: string;
  asset_id: string;
  agent_name: string;
  prompt_hash: string;
  gpt_mode: string;
  latency_ms: number;
  [key: string]: unknown;
}

export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
}

export interface CandlesResponse {
  candles: Candle[];
  nextCursor?: string;
}

export interface JournalListResponse {
  entries: JournalRow[];
  nextCursor?: string;
}

export interface AgentBrokerConfig {
  live_trading: boolean;
}

export interface AgentPromptConfig {
  [key: string]: string;
}

export interface AgentRiskConfig {
  max_drawdown: number;
  risk_per_trade: number;
}

export interface AgentOptionResolverConfig {
  [key: string]: string | number | boolean;
}

export interface Mt5Config {
  server: string;
  loginId: string;
  password: string;
  walletUrl?: string;
}

export interface BrokerConfig {
  mt5?: Mt5Config;
}

export type BrokerType = 'mt5';


export interface GptConfig {
  api_key?: string;
  prompt_id?: string;
}

export interface FeatureFlagsConfig {
  enable_backtest: boolean;
  enable_stream: boolean;
}

export interface AdminConfig {
  env: string;
  created_at: string;
}

export interface UiPrefsConfig {
  theme: string;
}

export interface AgentsConfig {
  candles_5_count: number;
  candles_15_count: number;
}

export interface DynamicConfig {
  ttl_minutes: number;
  candles_5m_count: number;
  candles_15m_count: number;
  reasoning: string;
  market_volatility: 'low' | 'medium' | 'high';
  trend_strength: 'weak' | 'moderate' | 'strong';
}

export interface MultiExpertSignal {
  signal: 'buy' | 'sell' | 'flat';
  entry: number | null; // null for flat signals
  stop: number | null;  // null for flat signals
  target: number | null; // null for flat signals
  ttl_minutes: number;
  confidence: number;
  active_window_c5: number;
  active_window_c15: number;
  need_more_candles: boolean;
  more_c5?: number;
  more_c15?: number;
  rationale?: string;
}

export interface CodeSniperSignal {
  code: string;
  input_schema: Record<string, any>;
  output_schema: Record<string, any>;
  metadata: Record<string, any>;
  ttl_minutes: number;
  confidence: number;
  dynamic_config?: DynamicConfig;
}

export interface AgentRunResponse {
  success: boolean;
  signal?: MultiExpertSignal | CodeSniperSignal;
  structured_metrics?: Record<string, any>;
  dynamic_config?: DynamicConfig;
  error?: string;
}

export const AgentInfoSchema = z.object({
  id: z.string(),
  symbol: z.string(),
  candle_count_5m: z.number().int().min(0),
  candle_count_15m: z.number().int().min(0),
  broker: z.string(),
  risk_percent: z.number().min(0).max(100).default(1.0),
  run_interval: z.string().default('hourly'),
  expiry_date: z.string().optional().nullable(),
  prompt_id: z.string().min(1, "Prompt ID is required for agent execution"),
})
export type AgentInfo = z.infer<typeof AgentInfoSchema>


export interface Mt5Wallet {
  login: number;
  name?: string | null;
  server?: string | null;
  currency?: string | null;
  balance: number;
  equity: number;
  margin: number;
  free_margin: number;
  leverage: number;
}

// Enhanced GPT Response interface with new fields
export interface EnhancedGptResponse {
  _id: string
  asset: string
  symbol: string  // New field
  agent_id: string
  assistant_id: string
  model: string
  prompt_hash: string
  prompt_content?: string  // Added for analyst-grade insights
  response_content: string
  response_type: string
  latency_ms: number
  tokens_used?: number
  thread_id?: string
  run_id?: string
  error_message?: string
  parsed_decision?: any
  confidence?: number
  pattern?: string
  direction?: string
  level?: number
  stop_loss?: number
  take_profit?: number
  quantity?: number
  broker_type?: string
  gpt_mode?: string
  timestamp: string
}

// Enhanced Trigger Check interface with new fields
export interface EnhancedTriggerCheck {
  _id: string
  asset: string
  symbol: string  // New field
  agent_id: string
  prompt_hash: string
  gpt_response_id: string
  trigger_condition: string
  trigger_type: string
  level?: number
  direction?: string
  buffer?: number
  trigger_met: boolean
  trigger_time?: string
  future_candles_count?: number
  entry_price?: number
  confidence?: number
  pattern?: string
  timestamp: string
}

// Enhanced GPT Call Summary interface with new fields
export interface EnhancedGptCallSummary {
  _id: string
  asset: string
  symbol: string  // New field
  agent_id: string
  timestamp: string
  prompt_hash: string
  gpt_response_id: string
  trigger_check_id?: string
  latency_ms: number
  tokens_used?: number
  response_type: string
  trigger_met?: boolean
  decision_made: boolean
  order_placed: boolean
  trade_executed: boolean
  confidence?: number
  pattern?: string
  direction?: string
  broker_type?: string
  gpt_mode?: string
}

// Slot-Candle Match interface
export interface SlotCandleMatch {
  _id: string
  asset: string
  slot_id: number
  slot_timestamp: string
  gpt_response_id: string
  trigger_check_id?: string
  level_drawn_at: string
  level_value: number
  direction: string
  trigger_met: boolean
  trigger_timestamp?: string
  trigger_candle_timestamp?: string
  trigger_candle_open?: number
  trigger_candle_high?: number
  trigger_candle_low?: number
  trigger_candle_close?: number
  trigger_candle_volume?: number
  entry_price?: number
  stop_loss?: number
  take_profit?: number
  confidence?: number
  pattern?: string
  agent_id: string
  prompt_hash: string
  broker_type?: string
  gpt_mode?: string
  trade_executed: boolean
  trade_id?: string
  timestamp: string
}

// Comprehensive Trade interface
export interface AICost {
  total_usd: number | null
  total_decimal_usd?: string | null
  known_subtotal_usd?: number
  currency: 'USD'
  status: 'priced' | 'unavailable'
  scope?: 'decision_cycle'
  cycle_id?: string
  calls?: Array<Record<string, unknown>>
}

export interface BrokerCost {
  total: number | null
  commission?: number
  swap?: number
  fee?: number
  currency?: string
  status: 'accrued' | 'final' | 'pending' | 'simulated'
  source?: string
  deal_ids?: string[]
}

export interface ComprehensiveTrade {
  _id: string
  symbol: string
  asset: string
  quantity: number
  price: number
  entry_price: number
  exit_price?: number
  stop_loss?: number
  take_profit?: number
  agent_id?: string
  prompt_hash?: string
  gpt_response_id?: string
  slot_candle_match_id?: string
  trade_type: 'paper' | 'live'
  side?: string
  pattern?: string
  confidence?: string
  broker_type?: string
  gpt_mode?: string
  signal_received_at: string
  trade_executed_at: string
  trade_closed_at?: string
  pnl?: number
  fees?: number
  trade_reason?: string | null
  trigger_reason?: string | null
  ai_cost?: AICost | null
  broker_cost?: BrokerCost | null
  slippage?: number
  trade_status: 'open' | 'closed' | 'cancelled'
  exit_reason?: string
  timestamp: string
}

// Hourly Candle interface
export interface HourlyCandle {
  _id: string
  asset: string
  symbol: string
  slot_id: number
  slot_timestamp: string
  open: number
  high: number
  low: number
  close: number
  volume?: number
  candle_timestamp: string
  candle_interval: '1h'
  market_session?: string
  gpt_signals_count: number
  triggers_met_count: number
  trades_executed_count: number
  timestamp: string
}

// Export response interfaces
export interface ExportResponse {
  message: string
  filepath: string
  format: string
  asset?: string
}

export interface ExportListResponse {
  exports: Array<{
    filename: string
    filepath: string
    size_bytes: number
    created_at: string
    modified_at: string
  }>
  count: number
  export_dir?: string
}

// Multi-Expert Cycles interfaces
export interface ExpertInsight {
  name: 'trend' | 'breakout' | 'reversal' | 'normal'
  market_analysis: Record<string, any>
  proposal: Record<string, any> | null
  validation: Record<string, any>
  passed: boolean
  error: string | null
  duration_ms: number | null  // number in TypeScript includes both int and float
}

export interface JudgeOutcome {
  selected: string | null
  rationale: string | null
  final_signal: Record<string, any> | null
  validation_summary: Record<string, any>
}

export interface MultiExpertCycle {
  _id: string
  cycle_id: string
  mode: 'multi_expert' | 'normal' | 'multi_expert_code_sniper'
  agent_id: string
  asset: string
  symbol: string
  started_at: string
  completed_at: string | null
  experts: ExpertInsight[]
  judge: JudgeOutcome | null
  status: 'success' | 'failed' | 'skipped' | 'flat' | 'completed'
  error: string | null
  version: number
}

export interface MultiExpertCyclesResponse {
  cycles: MultiExpertCycle[]
  count: number
}

export interface MultiExpertCyclesExportResponse {
  message: string
  filepath: string
  format: string
  asset: string
  count: number
}

// Backtest visualization types
export interface BacktestCandle {
  time: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface BacktestCandlesResponse {
  asset: string
  interval: string
  candles: BacktestCandle[]
  start_date: string
  end_date: string
}

export interface MonitoringWindow {
  start_time: string
  ttl_expiry: string
  code: string
  historical_candles: BacktestCandle[]
}

// Backtest iteration types (matching backend canonical BacktestPrediction)
export interface BacktestPrediction {
  // Canonical field names (single source of truth)
  direction?: string | null
  entry?: number | null  // Canonical name (not entry_price, level)
  stop?: number | null   // Canonical name (not stoploss, stop_loss)
  target?: number | null // Canonical name (not take_profit_1)
  pattern?: string | null
  confidence?: string | number | null
  status?: string | null
  // Code sniper specific fields
  code_executed?: boolean | null
  triggered_at_candle?: number | null
  entry_timestamp?: string | null
  [key: string]: unknown // Allow extra fields for flexibility
}

export interface BacktestExecutionResult {
  // Canonical field names (matching backend)
  direction?: string | null
  entry?: number | null
  stop?: number | null  // Canonical name (not stop_loss)
  target?: number | null  // Canonical name (not take_profit_1)
  confidence?: number | string | null
  rationale?: string | null
  triggered_candle_time?: string | null
  [key: string]: unknown // Allow extra fields for flexibility
}

export interface BacktestTradeOutcome {
  direction?: string | null
  entry?: number | null
  stop?: number | null
  initial_stop?: number | null
  target?: number | null
  risk_reward?: number | null
  entry_time?: string | null
  exit_time?: string | null
  exit_price?: number | null
  profit?: number
  result?: "TP" | "SL" | "BE" | "Timeout" | "No Entry" | "Open" | string
  signal_time?: string | null
  [key: string]: unknown // Allow extra fields for flexibility
}

export interface BacktestIteration {
  _id?: string
  backtest_id: string
  iteration: number
  iteration_timestamp: string
  signal_data?: Record<string, unknown>
  prediction?: BacktestPrediction
  execution_result?: BacktestExecutionResult | null
  trade_outcome?: BacktestTradeOutcome
  metrics?: Record<string, unknown>
  cycle_id?: string | null
  cycle_data?: Record<string, unknown>
  status?: "completed" | "skipped" | "error"
  error?: string | null
  timestamp?: string
  monitoring_active?: boolean
  ttl_expiry?: string
  [key: string]: unknown // Allow extra fields for flexibility
}

export interface BacktestResponse {
  backtest_id: string
  test_name: string
  asset: string
  start_date: string
  end_date: string
  status: string
  iterations?: number
  total_signals?: number
  wins?: number
  losses?: number
  total_pnl?: number
  max_drawdown?: number
  win_rate?: number
  avg_rr?: number
  created_at: string
  completed_at?: string | null
  error_message?: string | null
}


