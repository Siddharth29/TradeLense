import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import Mt5WalletPanel from '../Mt5WalletPanel'
import { toast } from 'sonner'
import { useMt5Wallet, useMt5Status, useAgentMetrics } from '@/services/hooks'
import { disconnectMt5 } from '@/services/apiClient'

vi.mock('@/services/apiClient', () => ({
  connectMt5: vi.fn(),
  disconnectMt5: vi.fn(),
  getBrokerConfig: vi.fn().mockResolvedValue({}),
}))

vi.mock('@/services/hooks', () => ({
  useMt5Wallet: vi.fn(),
  useMt5Status: vi.fn(),
  useAgentMetrics: vi.fn(),
}))

vi.mock('sonner', () => ({
  toast: { error: vi.fn(), success: vi.fn() },
}))

const renderPanel = () => {
  const client = new QueryClient()
  render(
    <QueryClientProvider client={client}>
      <Mt5WalletPanel />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  ;(useMt5Status as any).mockReturnValue({ data: { connected: true }, refetch: vi.fn() })
  ;(useAgentMetrics as any).mockReturnValue({ data: {} })
  ;(useMt5Wallet as any).mockReturnValue({
    data: {
      login: 1,
      name: 'acct',
      server: 'srv',
      currency: 'USD',
      balance: 1507.77,
      equity: 1507.77,
      margin: 0,
      free_margin: 1507.77,
      leverage: 100,
    },
    error: null,
  })
})

describe('Mt5WalletPanel', () => {
  it('renders connection status and balances', () => {
    renderPanel()
    expect(screen.getByText('MT5 Account')).toBeInTheDocument()
    expect(screen.getByText('Connected')).toBeInTheDocument()
    expect(screen.getByText('Balance')).toBeInTheDocument()
    // Currency-formatted balance.
    expect(screen.getAllByText('$1,507.77').length).toBeGreaterThan(0)
  })

  it('renders placeholders on wallet error', () => {
    ;(useMt5Wallet as any).mockReturnValue({ data: undefined, error: new Error('bad') })
    renderPanel()
    // Balance/margin etc. collapse to an em-dash when the wallet errors.
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })

  it('aborts disconnect while agents are running', () => {
    ;(useAgentMetrics as any).mockReturnValue({ data: { a1: { running: true } } })
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: /disconnect/i }))
    expect(toast.error).toHaveBeenCalledWith('Stop all agents before disconnecting broker')
    expect(disconnectMt5).not.toHaveBeenCalled()
  })
})
