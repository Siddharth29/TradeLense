import { create } from 'zustand'
import { useEffect } from 'react'
import { subscribe } from '../services/eventClient'

export interface AgentEvent {
  asset: string
  type: string
  [key: string]: unknown
}

interface EventsState {
  events: AgentEvent[]
  max: number
  add: (evt: AgentEvent) => void
  clear: () => void
}

const MAX_EVENTS = 100

export const useEventsStore = create<EventsState>((set) => ({
  events: [],
  max: MAX_EVENTS,
  add: (evt) =>
    set((s) => ({ events: [...s.events, evt].slice(-s.max) })),
  clear: () => set({ events: [] }),
}))

export const useAgentEvents = (asset?: string) => {
  const events = useEventsStore((s) =>
    asset ? s.events.filter((e) => e.asset === asset) : s.events
  )

  useEffect(() => {
    const handler = (evt: AgentEvent) => useEventsStore.getState().add(evt)
    const unsub = subscribe(handler)
    return () => unsub()
  }, [])

  return events
}
