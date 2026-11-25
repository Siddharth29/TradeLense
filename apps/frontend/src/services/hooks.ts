import type { UseQueryOptions } from "@tanstack/react-query";
import type { Backtest } from "@/types/backtest";
import {
  useQuery,
  useInfiniteQuery,
  useMutation,
  useQueryClient,
} from '@tanstack/react-query';
import {
  listAgents,
  getAgentMetrics,
  getAgentStatus,
  listReports,
  getReport,
  listFlags,
  getReadiness,
  getHealth,
  getMt5Wallet,
  getMt5Status,
  listConfigVersions,
  rollbackConfig,
  listBacktests,
  getBacktest,
  getBacktestIterations,
  deleteBacktest,
  stopBacktest,
  restartBacktest,
} from './apiClient';

export const useAgents = () =>
  useQuery({ queryKey: ['agents'], queryFn: listAgents });

export const useAgentMetrics = () =>
  useQuery({ queryKey: ['agent-metrics'], queryFn: getAgentMetrics });

export const useAgentStatus = (asset: string) =>
  useQuery({
    queryKey: ['agent-status', asset],
    queryFn: () => getAgentStatus(asset),
    enabled: !!asset,
  });

export const useReports = () =>
  useQuery({ queryKey: ['reports'], queryFn: listReports });

export const useReport = (testName: string) =>
  useQuery({
    queryKey: ['report', testName],
    queryFn: () => getReport(testName),
    enabled: !!testName,
  });

export const useFlags = () =>
  useQuery({ queryKey: ['flags'], queryFn: listFlags });

export const useReadiness = () =>
  useQuery({ queryKey: ['readiness'], queryFn: getReadiness });

export const useHealth = () =>
  useQuery({ queryKey: ['health'], queryFn: getHealth });

export const useMt5Wallet = (enabled = true) =>
  useQuery({
    queryKey: ['mt5-wallet'],
    queryFn: getMt5Wallet,
    enabled,
    staleTime: 15000,
    refetchInterval: (query) => (query.state.data ? 15000 : false),
  });

export const useMt5Status = () =>
  useQuery({
    queryKey: ['mt5-status'],
    queryFn: getMt5Status,
    staleTime: 15000,
    refetchInterval: 15000,
  });

export const useConfigVersions = (
  domain: string,
  env: string,
  enabled = true
) =>
  useInfiniteQuery({
    queryKey: ['configHistory', domain, env],
    enabled,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => listConfigVersions(domain, env, pageParam),
    getNextPageParam: (last) => last.nextCursor,
  });

export const useConfigRollback = (
  domain: string,
  env: string,
  queryKey: unknown[]
) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (version: number) => rollbackConfig(domain, env, version),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey });
      qc.invalidateQueries({ queryKey: ['configHistory', domain, env] });
    },
  });
};

// Backtest hooks
export const useBacktestList = (options?: {
  refetchInterval?: UseQueryOptions<Backtest[]>['refetchInterval'];
  enabled?: boolean;
}) => {
  return useQuery({
    queryKey: ['backtests', 'list'],
    queryFn: () => listBacktests(),
    staleTime: 5000,
    refetchInterval: options?.refetchInterval ?? false,
    enabled: options?.enabled ?? true,
    retry: 3,
    retryDelay: (attemptIndex) => Math.min(1000 * 2 ** attemptIndex, 30000),
  });
};

export const useBacktestDetails = (backtestId: string | undefined) => {
  return useQuery({
    queryKey: ['backtest', backtestId],
    queryFn: () => {
      if (!backtestId) throw new Error('Backtest ID required');
      return getBacktest(backtestId);
    },
    enabled: !!backtestId,
    staleTime: 3000,
    retry: 3,
    retryDelay: (attemptIndex) => Math.min(1000 * 2 ** attemptIndex, 30000),
  });
};

export const useBacktestIterations = (
  backtestId: string | undefined,
  limit: number = 500,
  options?: {
    refetchInterval?: number | false;
  }
) => {
  return useQuery({
    queryKey: ['backtest', backtestId, 'iterations', limit],
    queryFn: () => {
      if (!backtestId) throw new Error('Backtest ID required');
      return getBacktestIterations(backtestId, limit);
    },
    enabled: !!backtestId,
    staleTime: 3000,
    refetchInterval: options?.refetchInterval ?? false,
    retry: 3,
    retryDelay: (attemptIndex) => Math.min(1000 * 2 ** attemptIndex, 30000),
  });
};

// Backtest mutation hooks
export const useStopBacktest = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (backtestId: string) => stopBacktest(backtestId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['backtests', 'list'] });
      qc.invalidateQueries({ queryKey: ['backtest'] });
    },
  });
};

export const useRestartBacktest = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (backtestId: string) => restartBacktest(backtestId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['backtests', 'list'] });
      qc.invalidateQueries({ queryKey: ['backtest'] });
    },
  });
};

export const useDeleteBacktest = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (backtestId: string) => deleteBacktest(backtestId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['backtests', 'list'] });
    },
  });
};
