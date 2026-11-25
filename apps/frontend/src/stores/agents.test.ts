import { describe, it, expect, beforeEach } from 'vitest'
import { useAgentsStore } from './agents'



describe('useAgentsStore', () => {
  beforeEach(() => {
    useAgentsStore.setState({ agents: [], metrics: {} })
  })

  it('adds, removes agents and sets metrics', () => {
    const { addAgent, removeAgent, setMetrics } = useAgentsStore.getState()
    addAgent({
      id: 'agent1',
      symbol: 'AAPL',
      candleCount5m: 1,
      candleCount15m: 1,
      riskPercent: 1,
      broker: 'MT5',
      runInterval: 'hourly',
    })
    setMetrics({ agent1: { pnl: 1, runs: 0, decisions: 0, orders: 0, wins: 0, losses: 0, budget_skips: 0, last_run: null, eta: null, summary: '', running: false, usage: null } })
    expect(useAgentsStore.getState().agents[0].id).toBe('agent1')
    expect(useAgentsStore.getState().metrics.agent1.pnl).toBe(1)
    removeAgent('agent1')
    expect(useAgentsStore.getState().agents).toHaveLength(0)
  })
})
