import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import Dashboard from '@/pages/Dashboard';

vi.mock('@/services/apiClient', () => ({
  getSystemMetrics: vi.fn().mockResolvedValue({
    equity_curve: [],
    daily_pnl: [],
    win_rate: 0.5,
  }),
  getAgentMetrics: vi.fn().mockResolvedValue({
    AAPL: {
      agent_id: 'AAPL',
      runs: 0,
      decisions: 0,
      orders: 0,
      wins: 0,
      losses: 0,
      pnl: 100,
      budget_skips: 0,
      last_run: null,
      eta: null,
      summary: '',
      running: true,
      usage: 0,
      drift_p50: null,
      drift_p95: 5,
      hit_rate: 1,
    },
  }),
}));

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: any) => <div>{children}</div>,
  LineChart: ({ children }: any) => <div>{children}</div>,
  Line: () => <div />,
  BarChart: ({ children }: any) => <div>{children}</div>,
  Bar: () => <div />,
  XAxis: () => <div />,
  YAxis: () => <div />,
  CartesianGrid: () => <div />,
  Tooltip: () => <div />,
  Cell: () => <div />,
  ReferenceLine: () => <div />,
  AreaChart: ({ children }: any) => <div>{children}</div>,
  Area: () => <div />,
  LabelList: () => <div />,
}));

describe('Dashboard', () => {
  it('displays system and agent metrics', async () => {
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <Dashboard />
      </QueryClientProvider>
    );

    expect(await screen.findByText('Dashboard')).toBeInTheDocument();
    expect((await screen.findAllByText('$100.00')).length).toBeGreaterThan(0);
    expect(await screen.findByText('Running Agents')).toBeInTheDocument();
    expect(await screen.findByText('Win Rate')).toBeInTheDocument();
  });
});
