import type { Backtest, Iteration } from "@/types/backtest";
import axios from 'axios';
import type { AxiosError, AxiosInstance } from 'axios';
import axiosRetry from 'axios-retry';
import type {
  AICost,
  BrokerCost,
  AgentStatus,
  Metrics,
  JournalRow,
  Mt5Config,
  BrokerConfig,
  Candle,
  CandlesResponse,
  SystemMetrics,
  HealthResponse,
  Flags,
  Readiness,
  FeatureFlagsConfig,
  AdminConfig,
  UiPrefsConfig,
  Mt5Wallet,
  AgentInfo,
  AgentsConfig,
  EnhancedGptResponse,
  EnhancedTriggerCheck,
  EnhancedGptCallSummary,
  SlotCandleMatch,
  ComprehensiveTrade,
  HourlyCandle,
  ExportResponse,
  ExportListResponse,
  ExpertInsight,
  JudgeOutcome,
  MultiExpertCycle,
  MultiExpertCyclesResponse,
  MultiExpertCyclesExportResponse,
} from '../types/api';
import { AgentInfoSchema } from '../types/api';

interface RequestMetadata {
  start: number;
  id: string;
  duration?: number;
}

declare module 'axios' {
  interface AxiosRequestConfig {
    metadata?: RequestMetadata;
  }
}

// Use the environment variable or fallback to a default URL. Respect empty string for tests.
export const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000';

console.log('API Client initialized with base URL:', API_BASE_URL);

const client: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  withCredentials: true, // Enable sending cookies in cross-origin requests
});

client.interceptors.request.use((config) => {
  // Generate a simple UUID-like ID that works in all environments
  const id = 'req_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
  config.headers = config.headers ?? {};
  config.headers['X-Request-ID'] = id;
  config.metadata = { start: Date.now(), id };
  return config;
});

client.interceptors.response.use(
  (response) => {
    const meta = response.config.metadata;
    if (meta) {
      meta.duration = Date.now() - meta.start;
    }
    return response;
  },
  (error: AxiosError) => {
    const meta = error.config?.metadata;
    if (meta) {
      meta.duration = Date.now() - meta.start;
    }
    const data = error.response?.data as { detail?: string } | undefined;
    const normalized = new Error(data?.detail ?? error.message) as Error & {
      status?: number;
    };
    normalized.status = error.response?.status;
    return Promise.reject(normalized);
  }
);

axiosRetry(client, {
  retries: 3,
  retryCondition: (err) => err.config?.method?.toLowerCase() === 'get',
  retryDelay: (count) => axiosRetry.exponentialDelay(count) + Math.random() * 100,
});

// API methods ---------------------------------------------------------------

export const listAgents = async (): Promise<{ agents: AgentInfo[] }> => {
  const res = await client.get<{ agents: unknown }>('/agents');
  return { agents: AgentInfoSchema.array().parse(res.data.agents) };
};

export const getSystemMetrics = () =>
  client.get<SystemMetrics>('/metrics').then((r) => r.data);

export const getAgentMetrics = () =>
  client.get<Record<string, Metrics>>('/metrics/agents').then((r) => r.data);

export const getHealth = () =>
  client.get<HealthResponse>('/health').then((r) => r.data);

export const getReadiness = () =>
  client.get<Readiness>('/readiness').then((r) => r.data);

export const listFlags = () =>
  client.get<Flags>('/flags').then((r) => r.data);

export const killSwitch = () => client.post('/kill').then((r) => r.data);

export const getAgentStatus = (symbol: string) =>
  client.get<AgentStatus>(`/agents/${symbol}/status`).then((r) => r.data);

export const startAgent = (symbol: string, cfg: Record<string, unknown>) =>
  client.post<{ status: string }>(`/agents/${symbol}/start`, cfg).then((r) => r.data);

export const stopAgent = (symbol: string) =>
  client.post<{ status: string }>(`/agents/${symbol}/stop`).then((r) => r.data);

export const runAgentNow = (symbol: string) =>
  client
    .post<{ result: unknown; config: Record<string, unknown> }>(
      `/agents/${symbol}/run-now`
    )
    .then((r) => r.data);

export const saveAgent = (agent: AgentInfo) => {
  const payload = AgentInfoSchema.parse(agent);
  return client
    .put<AgentInfo>(`/agents/${agent.id}`, payload)
    .then((r) => AgentInfoSchema.parse(r.data));
};

export const deleteAgent = (id: string) =>
  client.delete(`/agents/${id}`).then((r) => r.data);

export interface Report {
  test_name: string;
  trades: JournalRow[];
}

export const createReport = (report: Report) =>
  client.post<{ status: string }>('/api/reports', report).then((r) => r.data);

export const listReports = () =>
  client.get<unknown[]>('/api/reports').then((r) => r.data);

export const getReport = (testName: string) =>
  client.get<unknown>(`/api/reports/${testName}`).then((r) => r.data);

// Config helpers -------------------------------------------------------------

export const SECRET_PLACEHOLDER = '********';

export const getBrokerConfig = () =>
  client.get<BrokerConfig>('/configs/brokers').then((r) => r.data);
export const putBrokerConfig = (cfg: BrokerConfig) =>
  client.put('/configs/brokers', cfg).then((r) => r.data);

export const testBrokerConfig = (
  broker: 'mt5',
  cfg: Mt5Config,
) =>
  client.post('/configs/brokers/test', { broker, config: cfg }).then((r) => r.data);



export const getMt5Wallet = () =>
  client.get<Mt5Wallet>('/brokers/mt5/wallet').then((r) => r.data);









export const connectMt5 = (cfg?: Mt5Config) => {
  if (cfg) {
    return client
      .post<{ status: string }>('/brokers/mt5/connect', cfg)
      .then((r) => r.data);
  }
  return client.get<{ status: string }>('/brokers/mt5/connect').then((r) => r.data);
};

export const getMt5Status = () =>
  client.get<{ connected: boolean; offline?: boolean }>('/brokers/mt5/status').then((r) => r.data);

export const disconnectMt5 = () =>
  client.post('/brokers/mt5/disconnect').then((r) => r.data);

export interface Mt5OrderPayload {
  symbol: string;
  side: string;
  quantity: number;
  order_type?: string;
  price?: number;
  sl?: number;
  tp?: number;
  client_order_id?: string;
}

export const placeMt5Order = (payload: Mt5OrderPayload) =>
  client.post('/brokers/mt5/orders', payload).then((r) => r.data);

export const modifyMt5Order = (orderId: string, payload: Mt5OrderPayload) =>
  client.put(`/brokers/mt5/orders/${orderId}`, payload).then((r) => r.data);

export const cancelMt5Order = (orderId: string) =>
  client.delete(`/brokers/mt5/orders/${orderId}`).then((r) => r.data);

export const getMt5Positions = () =>
  client.get('/brokers/mt5/positions').then((r) => r.data);

export const getMt5Balances = () =>
  client.get('/brokers/mt5/balances').then((r) => r.data);

export const getMt5SymbolInfo = (symbol: string) =>
  client
    .get('/brokers/mt5/symbol-info', { params: { symbol } })
    .then((r) => r.data);

export const getAgentsConfig = () =>
  client.get<AgentsConfig>('/configs/agents').then((r) => r.data);
export const putAgentsConfig = (cfg: AgentsConfig) =>
  client.put('/configs/agents', cfg).then((r) => r.data);




export const getFeatureFlagsConfig = () =>
  client.get<FeatureFlagsConfig>('/configs/flags').then((r) => r.data);
export const putFeatureFlagsConfig = (cfg: FeatureFlagsConfig) =>
  client.put('/configs/flags', cfg).then((r) => r.data);

export const getAdminConfig = () =>
  client.get<AdminConfig>('/configs/admin').then((r) => r.data);
export const putAdminConfig = (cfg: AdminConfig) =>
  client.put('/configs/admin', cfg).then((r) => r.data);

export const getUiPrefsConfig = () =>
  client.get<UiPrefsConfig>('/configs/ui').then((r) => r.data);
export const putUiPrefsConfig = (cfg: UiPrefsConfig) =>
  client.put('/configs/ui', cfg).then((r) => r.data);

// Config version history ------------------------------------------------------

export interface ConfigVersion {
  version: number;
  doc: unknown;
  updated_at?: string;
}

export interface ConfigVersionPage {
  versions: ConfigVersion[];
  nextCursor?: string;
}

export const listConfigVersions = (
  domain: string,
  env: string,
  cursor?: string,
) =>
  client
    .get<ConfigVersionPage>(`/configs/${domain}/versions`, {
      params: { env, cursor },
    })
    .then((r) => r.data);

export const rollbackConfig = (
  domain: string,
  env: string,
  version: number,
) => client.post(`/configs/${domain}/rollback`, { env, version }).then((r) => r.data);

// Enhanced GPT Analysis API functions
export const getEnhancedGptResponses = async (
  asset: string = 'ALL',
  limit: number = 50,
  skip: number = 0,
) => {
  const params = new URLSearchParams({
    asset,
    limit: limit.toString(),
    skip: skip.toString(),
  });
  
  return client
    .get(`/api/gpt-responses?${params}`)
    .then((r) => r.data as { responses: EnhancedGptResponse[]; count: number });
};

export const getEnhancedTriggerChecks = async (
  asset: string = 'ALL',
  limit: number = 50,
  skip: number = 0,
) => {
  const params = new URLSearchParams({
    asset,
    limit: limit.toString(),
    skip: skip.toString(),
  });
  
  return client
    .get(`/api/trigger-checks?${params}`)
    .then((r) => r.data as { checks: EnhancedTriggerCheck[]; count: number });
};

export const getEnhancedGptCallSummaries = async (
  asset: string = 'ALL',
  limit: number = 50,
  skip: number = 0,
) => {
  const params = new URLSearchParams({
    asset,
    limit: limit.toString(),
    skip: skip.toString(),
  });
  
  return client
    .get(`/api/gpt-call-summaries?${params}`)
    .then((r) => r.data as { summaries: EnhancedGptCallSummary[]; count: number });
};

// Slot-Candle Match API functions
export const getSlotCandleMatches = async (
  asset: string,
  limit: number = 50,
  skip: number = 0,
) => {
  const params = new URLSearchParams({
    limit: limit.toString(),
    skip: skip.toString(),
  });
  
  return client
    .get(`/api/slot-candle-matches/${asset}?${params}`)
    .then((r) => r.data as { matches: SlotCandleMatch[]; count: number });
};

// Comprehensive Trades API functions
export const getComprehensiveTrades = async (
  asset: string = 'ALL',
  limit: number = 50,
  skip: number = 0,
) => {
  const params = new URLSearchParams({
    asset,
    limit: limit.toString(),
    skip: skip.toString(),
  });
  
  return client
    .get(`/api/comprehensive-trades?${params}`)
    .then((r) => r.data as { trades: ComprehensiveTrade[]; count: number });
};

// Hourly Candles API functions
export const getHourlyCandles = async (
  asset: string,
  limit: number = 50,
  skip: number = 0,
) => {
  const params = new URLSearchParams({
    limit: limit.toString(),
    skip: skip.toString(),
  });
  
  return client
    .get(`/api/hourly-candles/${asset}?${params}`)
    .then((r) => r.data as { candles: HourlyCandle[]; count: number });
};

// Export API functions
export const exportGptResponses = async (
  asset: string = 'ALL',
  format: 'json' | 'csv' = 'json',
  startTime?: string,
  endTime?: string,
) => {
  const params = new URLSearchParams({
    asset,
    format,
  });
  
  if (startTime) params.append('start_time', startTime);
  if (endTime) params.append('end_time', endTime);
  
  return client
    .post(`/api/export/gpt-responses?${params}`)
    .then((r) => r.data as ExportResponse);
};

export const exportComprehensiveTrades = async (
  asset: string = 'ALL',
  format: 'json' | 'csv' = 'json',
  startTime?: string,
  endTime?: string,
) => {
  const params = new URLSearchParams({
    asset,
    format,
  });
  
  if (startTime) params.append('start_time', startTime);
  if (endTime) params.append('end_time', endTime);
  
  return client
    .post(`/api/export/comprehensive-trades?${params}`)
    .then((r) => r.data as ExportResponse);
};

export const exportSlotCandleMatches = async (
  asset: string = 'ALL',
  format: 'json' | 'csv' = 'json',
  startTime?: string,
  endTime?: string,
) => {
  const params = new URLSearchParams({
    asset,
    format,
  });
  
  if (startTime) params.append('start_time', startTime);
  if (endTime) params.append('end_time', endTime);
  
  return client
    .post(`/api/export/slot-candle-matches?${params}`)
    .then((r) => r.data as ExportResponse);
};

export const exportTradingReport = async (
  asset: string = 'ALL',
  format: 'json' | 'csv' = 'json',
  startTime?: string,
  endTime?: string,
) => {
  const params = new URLSearchParams({
    asset,
    format,
  });
  
  if (startTime) params.append('start_time', startTime);
  if (endTime) params.append('end_time', endTime);
  
  return client
    .post(`/api/export/trading-report?${params}`)
    .then((r) => r.data as ExportResponse);
};

export const exportGptSniperLevels = async (
  asset: string = 'ALL',
  format: 'json' | 'csv' = 'json',
  startTime?: string,
  endTime?: string,
) => {
  const params = new URLSearchParams({
    asset,
    format,
  });
  
  if (startTime) params.append('start_time', startTime);
  if (endTime) params.append('end_time', endTime);
  
  return client
    .post(`/api/export/gpt-sniper-levels?${params}`)
    .then((r) => r.data as ExportResponse);
};

export const listExports = async () => {
  return client
    .get('/api/export/list')
    .then((r) => r.data as ExportListResponse);
};

export const getAvailableAssets = async () => {
  return client
    .get('/api/available-assets')
    .then((r) => r.data as { assets: string[]; count: number });
};

// Prompt Management API functions
export const listPrompts = async (): Promise<any[]> => {
  const response = await client.get('/prompts/');
  return response.data;
};

export const getPrompt = async (promptId: string): Promise<any> => {
  const response = await client.get(`/prompts/${promptId}`);
  return response.data;
};

export const createPrompt = async (request: any): Promise<{ id: string; message: string }> => {
  const response = await client.post('/prompts/', request);
  return response.data;
};

export const updatePrompt = async (promptId: string, request: any): Promise<{ message: string }> => {
  const response = await client.put(`/prompts/${promptId}`, request);
  return response.data;
};

export const deletePrompt = async (promptId: string): Promise<{ message: string }> => {
  const response = await client.delete(`/prompts/${promptId}`);
  return response.data;
};

export const searchPrompts = async (query: string): Promise<any[]> => {
  const response = await client.get(`/prompts/search/${encodeURIComponent(query)}`);
  return response.data;
};

// Model Configuration API functions
export type ReasoningEffort = 'low' | 'medium' | 'high' | 'xhigh' | 'max';

export interface ModelConfig {
  model: string;
  temperature: number;
  top_p: number;
  max_tokens: number;
  effort?: ReasoningEffort;
}

export const getModelConfig = async (): Promise<{ config: ModelConfig; checksum: string | null }> => {
  const response = await client.get('/configs/model-config');
  return response.data;
};

export const saveModelConfig = async (config: ModelConfig): Promise<{ status: string; config: ModelConfig }> => {
  const response = await client.put('/configs/model-config', config);
  return response.data;
};

// Multi-Expert Cycles API functions
export const getMultiExpertCycles = async (
  agent_id?: string,
  asset?: string,
  mode?: string,
  status?: string,
  start_time?: string,
  end_time?: string,
  limit: number = 50,
  skip: number = 0,
) => {
  const params = new URLSearchParams();
  if (agent_id) params.append('agent_id', agent_id);
  if (asset) params.append('asset', asset);
  if (mode) params.append('mode', mode);
  if (status) params.append('status', status);
  if (start_time) params.append('start_time', start_time);
  if (end_time) params.append('end_time', end_time);
  params.append('limit', limit.toString());
  params.append('skip', skip.toString());
  
  return client
    .get(`/api/multi-expert/cycles?${params}`)
    .then((r) => r.data as MultiExpertCyclesResponse);
};

export const getMultiExpertCycle = async (cycle_id: string) => {
  return client
    .get(`/api/multi-expert/cycles/${cycle_id}`)
    .then((r) => r.data as MultiExpertCycle);
};

// Backtest API functions
export const createBacktest = async (data: {
  test_name: string;
  asset: string;
  start_date: string;
  end_date: string;
  candle_count_5m: number;
  candle_count_15m: number;
  use_code_sniper: boolean;
  interval_hours: number;
}) => {
  return client.post('/api/backtest/run', data).then((r) => r.data);
};

export const getBacktest = async (backtestId: string) => {
  return client.get<Backtest>(`/api/backtest/${backtestId}`).then((r) => r.data);
};

export const listBacktests = async (params?: {
  asset?: string;
  start_date?: string;
  end_date?: string;
  limit?: number;
}) => {
  const queryParams = new URLSearchParams();
  if (params?.asset) queryParams.append('asset', params.asset);
  if (params?.start_date) queryParams.append('start_date', params.start_date);
  if (params?.end_date) queryParams.append('end_date', params.end_date);
  if (params?.limit) queryParams.append('limit', params.limit.toString());
  
  return client.get<Backtest[]>(`/api/backtest/list?${queryParams}`).then((r) => r.data);
};

export const getBacktestIterations = async (backtestId: string, limit: number = 100) => {
  return client
    .get<Iteration[]>(`/api/backtest/${backtestId}/iterations?limit=${limit}`)
    .then((r) => r.data);
};

export const getBacktestSummary = async (backtestId: string) => {
  return client.get(`/api/backtest/${backtestId}/summary`).then((r) => r.data);
};

export const deleteBacktest = async (backtestId: string) => {
  return client.delete(`/api/backtest/${backtestId}`).then((r) => r.data);
};

export const stopBacktest = async (backtestId: string) => {
  return client.post(`/api/backtest/${backtestId}/stop`).then((r) => r.data);
};

export const restartBacktest = async (backtestId: string) => {
  return client.post(`/api/backtest/${backtestId}/restart`).then((r) => r.data);
};

// Candle Management API functions
export const fetchHistoricalCandles = async (data: {
  asset: string;
  start_date: string;
  end_date: string;
  intervals?: string[];
}) => {
  return client.post('/api/backtest/fetch-historical-data', data).then((r) => r.data);
};

export const getCandleStats = async (asset: string, params?: {
  start_date?: string;
  end_date?: string;
}) => {
  const queryParams = new URLSearchParams();
  if (params?.start_date) queryParams.append('start_date', params.start_date);
  if (params?.end_date) queryParams.append('end_date', params.end_date);
  
  return client
    .get(`/api/backtest/candles/stats/${asset}?${queryParams}`)
    .then((r) => r.data);
};

export const countCandles = async (
  asset: string,
  interval: '5m' | '15m',
  params?: {
    start_date?: string;
    end_date?: string;
  }
) => {
  const queryParams = new URLSearchParams({ interval });
  if (params?.start_date) queryParams.append('start_date', params.start_date);
  if (params?.end_date) queryParams.append('end_date', params.end_date);
  
  return client
    .get(`/api/backtest/candles/count/${asset}?${queryParams}`)
    .then((r) => r.data);
};

export const deleteAllCandles = async (asset: string, interval?: '5m' | '15m') => {
  const queryParams = new URLSearchParams();
  if (interval) queryParams.append('interval', interval);
  
  return client
    .delete(`/api/backtest/candles/${asset}?${queryParams}`)
    .then((r) => r.data);
};

export const deleteCandlesRange = async (
  asset: string,
  start_date: string,
  end_date: string,
  interval?: '5m' | '15m'
) => {
  const queryParams = new URLSearchParams({
    start_date,
    end_date,
  });
  if (interval) queryParams.append('interval', interval);
  
  return client
    .delete(`/api/backtest/candles/${asset}/range?${queryParams}`)
    .then((r) => r.data);
};

export const exportMultiExpertCycles = async (
  asset: string = 'ALL',
  format: 'json' | 'csv' = 'json',
  start_time?: string,
  end_time?: string,
) => {
  const params = new URLSearchParams({
    asset,
    format,
  });
  
  if (start_time) params.append('start_time', start_time);
  if (end_time) params.append('end_time', end_time);
  
  return client
    .post(`/api/export/multi-expert-cycles?${params}`)
    .then((r) => r.data as MultiExpertCyclesExportResponse);
};

// Delete API methods
export const deleteGptResponse = async (id: string): Promise<{ message: string }> => {
  return client
    .delete(`/api/gpt-responses/${id}`)
    .then((r) => r.data);
};

export const deleteTriggerCheck = async (id: string): Promise<{ message: string }> => {
  return client
    .delete(`/api/trigger-checks/${id}`)
    .then((r) => r.data);
};

export const deleteComprehensiveTrade = async (id: string): Promise<{ message: string }> => {
  return client
    .delete(`/api/comprehensive-trades/${id}`)
    .then((r) => r.data);
};

export const deleteSlotCandleMatch = async (id: string): Promise<{ message: string }> => {
  return client
    .delete(`/api/slot-candle-matches/${id}`)
    .then((r) => r.data);
};

export const deleteMultiExpertCycle = async (cycleId: string): Promise<{ message: string }> => {
  return client
    .delete(`/api/multi-expert/cycles/${cycleId}`)
    .then((r) => r.data);
};

export const getCycleById = async (cycleId: string): Promise<MultiExpertCycle | null> => {
  const response = await getMultiExpertCycles(undefined, undefined, undefined, undefined, undefined, undefined, 100, 0);
  return response.cycles.find(c => c.cycle_id === cycleId) || null;
};

export const getGptResponseById = async (responseId: string): Promise<EnhancedGptResponse | null> => {
  const response = await getEnhancedGptResponses('ALL', 100, 0);
  return response.responses.find(r => r._id === responseId) || null;
};

export type {
  AgentStatus,
  Metrics,
  JournalRow,
  Mt5Config,
  BrokerConfig,
  Candle,
  CandlesResponse,
  FeatureFlagsConfig,
  AdminConfig,
  UiPrefsConfig,
  Mt5Wallet,
  EnhancedGptResponse,
  EnhancedTriggerCheck,
  EnhancedGptCallSummary,
  SlotCandleMatch,
  ComprehensiveTrade,
  HourlyCandle,
  ExportResponse,
  ExportListResponse,
  ExpertInsight,
  JudgeOutcome,
  MultiExpertCycle,
  MultiExpertCyclesResponse,
  MultiExpertCyclesExportResponse,
};

// ---------------------------------------------------------------------------
// Trading Analysis API
// ---------------------------------------------------------------------------

export interface TradingAnalysisTrade {
  id: string
  asset: string
  symbol: string
  side: string | null
  entry_price: number | null
  exit_price: number | null
  stop_loss: number | null
  take_profit: number | null
  sl_trail: Array<{ sl: number | null; label: string | null; at: string | null }>
  quantity: number | null
  pnl: number | null
  pnl_in_parent?: boolean
  trade_reason?: string | null
  trigger_reason?: string | null
  ai_cost?: AICost | null
  broker_cost?: BrokerCost | null
  cycle_id?: string | null
  position_ticket?: string | null
  parent_trade_id?: string | null
  trade_status: string
  exit_reason: string | null
  pattern: string | null
  confidence: string | null
  trade_type: string
  broker_type: string | null
  gpt_mode: string | null
  signal_received_at: string | null
  trade_executed_at: string | null
  trade_closed_at: string | null
  gpt_response_id: string | null
  decision_flow: {
    cycle_id: string
    mode: string
    status: string
    started_at: string | null
    completed_at: string | null
    experts: Array<{
      name: string
      passed: boolean
      proposal: Record<string, any> | null
      market_analysis: Record<string, any>
      validation: Record<string, any>
      duration_ms: number | null
    }>
    judge: {
      selected: string | null
      rationale: string | null
      final_signal: Record<string, any> | null
      validation_summary: Record<string, any>
    } | null
  } | null
}

export interface TradingAnalysisResponse {
  trades: TradingAnalysisTrade[]
  count: number
}

export const getTradingAnalysisTrades = async (
  asset?: string,
  status?: string,
  side?: string,
  limit: number = 50,
  start?: string,
  end?: string,
): Promise<TradingAnalysisResponse> => {
  const params = new URLSearchParams();
  if (asset) params.append('asset', asset);
  if (status) params.append('status', status);
  if (side) params.append('side', side);
  if (start) params.append('start', start);
  if (end) params.append('end', end);
  params.append('limit', limit.toString());

  return client
    .get(`/api/trading-analysis/trades?${params}`)
    .then((r) => r.data as TradingAnalysisResponse);
};

export const deleteTradingAnalysisTrade = async (
  tradeId: string,
): Promise<{ deleted: boolean; trade_id: string; cycle_deleted: boolean }> => {
  return client
    .delete(`/api/trading-analysis/trades/${tradeId}`)
    .then((r) => r.data);
};

export const deleteAllTradingAnalysisTrades = async (): Promise<{
  deleted: boolean;
  deleted_count: number;
  by_collection: Record<string, number>;
}> => {
  return client
    .delete('/api/trading-analysis/purge')
    .then((r) => r.data);
};

