import { create } from 'zustand'
import type { Metrics } from '@/types/api'

export interface Agent {
  id: string
  symbol: string
  candleCount5m: number
  candleCount15m: number
  broker: string
  riskPercent: number
  runInterval: string
  promptId?: string
}

interface AgentsState {
  agents: Agent[]
  metrics: Record<string, Metrics>
  setAgents: (agents: Agent[]) => void
  addAgent: (agent: Agent) => void
  removeAgent: (id: string) => void
  setMetrics: (metrics: Record<string, Metrics>) => void
}

export const useAgentsStore = create<AgentsState>((set) => ({
  agents: [],
  metrics: {},
  setAgents: (agents) => set({ agents }),
  addAgent: (agent) =>
    set((state) => ({ agents: [...state.agents, agent] })),
  removeAgent: (id) =>
    set((state) => ({ agents: state.agents.filter((a) => a.id !== id) })),
  setMetrics: (metrics) => set({ metrics }),
}))

export const useAgentList = () => useAgentsStore((s) => s.agents)
export const useAgentMetrics = (id: string) =>
  useAgentsStore((s) => s.metrics[id])
