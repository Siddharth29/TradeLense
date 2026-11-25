import type { AgentEvent } from '../stores/events'
import { API_BASE_URL } from './apiClient'

export type EventHandler<T> = (evt: T) => void

export interface EventClient<T> {
  subscribe: (cb: EventHandler<T>) => () => void
}

export const createEventClient = <T>(url: string): EventClient<T> => {
  let worker: Worker | null = null
  const listeners = new Set<EventHandler<T>>()
  const DEBUG = import.meta.env.DEV || import.meta.env.VITE_DEBUG_EVENTS === 'true'

  const ensureWorker = () => {
    if (!worker) {
      worker = new Worker(new URL('./eventWorker.ts', import.meta.url), {
        type: 'module',
      })
      worker.addEventListener('message', (e: MessageEvent) => {
        const { type, payload } = e.data as { type: string; payload: T | any }
        if (type === 'event') {
          for (const cb of listeners) cb(payload)
        } else if (type === 'log' && DEBUG) {
          console.log(`[EventClient] ${payload.message}`, payload.data || '')
        }
      })
      
      worker.addEventListener('error', (e) => {
        console.error('[EventClient] Worker error:', e)
      })
    }
  }

  const start = () => {
    ensureWorker()
    if (DEBUG) console.log(`[EventClient] Starting connection to ${url}`)
    worker!.postMessage({ type: 'start', url })
  }

  const stop = () => {
    if (DEBUG) console.log('[EventClient] Stopping connection')
    worker?.postMessage({ type: 'stop' })
  }

  const subscribe = (cb: EventHandler<T>) => {
    listeners.add(cb)
    if (listeners.size === 1) start()
    return () => {
      listeners.delete(cb)
      if (listeners.size === 0) stop()
    }
  }

  return { subscribe }
}

const EVENTS_URL = `${API_BASE_URL}/events`
const eventsClient = createEventClient<AgentEvent>(EVENTS_URL)
export const subscribe = eventsClient.subscribe
