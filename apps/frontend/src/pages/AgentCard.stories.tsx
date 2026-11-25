import type { Meta, StoryObj } from '@storybook/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AgentCard } from './agents';
import { useAgentsStore } from '@/stores/agents';

const meta: Meta<typeof AgentCard> = {
  title: 'Components/AgentCard',
  component: AgentCard,
};
export default meta;

type Story = StoryObj<typeof AgentCard>;

export const Default: Story = {
  args: {
    agent: {
      id: 'agent1',
      symbol: 'AAPL',
      candleCount5m: 120,
      candleCount15m: 80,
      broker: 'mt5',
      riskPercent: 5,
      runInterval: 'hourly',
    },
  },
  render: (args) => {
    useAgentsStore.setState({
      metrics: {
        AAPL: {
          runs: 3,
          decisions: 0,
          orders: 2,
          wins: 1,
          losses: 0,
          pnl: 12.5,
          realized_pnl: 8.5,
          unrealized_pnl: 4,
          open_trades: 1,
          budget_skips: 0,
          last_run: new Date().toISOString(),
          eta: new Date(Date.now() + 3600_000).toISOString(),
          summary: 'ok',
          running: true,
          usage: 0.5,
        },
      },
    });
    const client = new QueryClient();
    return (
      <QueryClientProvider client={client}>
        <AgentCard {...args} />
      </QueryClientProvider>
    );
  },
};
