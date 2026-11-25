import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AgentCard } from '@/pages/agents';
import { useAgentsStore } from '@/stores/agents';
import { TooltipProvider } from '@/components/ui/tooltip';
import { toast } from 'sonner';
import { useMt5Status } from '@/services/hooks';

vi.mock('@/services/hooks', () => ({
  useMt5Status: vi.fn(),
}));

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

const agent = {
  id: 'agent1',
  symbol: 'AAPL',
  candleCount5m: 1,
  candleCount15m: 1,
  broker: 'mt5',
  riskPercent: 5,
  runInterval: 'hourly',
};

const renderCard = () => {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <AgentCard agent={agent} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
};

beforeEach(() => {
  vi.clearAllMocks();
  (useMt5Status as any).mockReturnValue({ data: { connected: true } });
});

describe('AgentCard', () => {
  it('renders running agent with metrics and a Stop control', () => {
    useAgentsStore.setState({
      metrics: {
        AAPL: {
          agent_id: 'agent1',
          runs: 3,
          decisions: 0,
          orders: 2,
          wins: 1,
          losses: 0,
          pnl: 12.5,
          budget_skips: 0,
          last_run: null,
          eta: null,
          summary: 'ok',
          running: true,
          usage: 0.5,
        } as any,
      },
    });

    renderCard();

    expect(screen.getByText('AAPL')).toBeInTheDocument();
    expect(screen.getByText('Running')).toBeInTheDocument();
    // Running agent exposes Stop (not Start) as the primary action.
    expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Start' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument();
  });

  it('blocks Start with a toast when broker is disconnected', () => {
    (useMt5Status as any).mockReturnValue({ data: { connected: false } });
    useAgentsStore.setState({ metrics: { AAPL: { running: false } as any } });

    renderCard();

    const startButton = screen.getByRole('button', { name: 'Start' });
    expect(startButton).not.toBeDisabled();
    fireEvent.click(startButton);
    expect(toast.error).toHaveBeenCalledWith('Connect broker before starting');
  });

  it('shows Start (not Stop) when the agent is stopped', () => {
    useAgentsStore.setState({ metrics: { AAPL: { running: false } as any } });

    renderCard();

    expect(screen.getByRole('button', { name: 'Start' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument();
  });
});
