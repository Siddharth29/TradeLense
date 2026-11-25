import type { Meta, StoryObj } from '@storybook/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import Dashboard from './Dashboard';

const meta: Meta<typeof Dashboard> = {
  title: 'Pages/Dashboard',
  component: Dashboard,
};
export default meta;

type Story = StoryObj<typeof Dashboard>;

export const Default: Story = {
  render: () => {
    const client = new QueryClient();
    client.setQueryData(['systemMetrics'], {
      equity_curve: [],
      daily_pnl: [],
      win_rate: 0.5,
    });
    client.setQueryData(['agentMetrics'], {
      AAPL: {
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
      },
    });
    return (
      <QueryClientProvider client={client}>
        <Dashboard />
      </QueryClientProvider>
    );
  },
};
