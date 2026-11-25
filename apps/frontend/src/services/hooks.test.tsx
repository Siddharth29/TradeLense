import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect } from 'vitest'
import { useAgents, useAgentStatus, useConfigVersions } from './hooks'

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>
)

describe('service hooks', () => {
  it('fetches agents', async () => {
    const { result } = renderHook(() => useAgents(), { wrapper })
    await waitFor(() => expect(result.current.data?.agents[0].id).toBe('AAPL'))
  })

  it('fetches agent status', async () => {
    const { result } = renderHook(() => useAgentStatus('AAPL'), { wrapper })
    await waitFor(() => expect(result.current.data?.running).toBe(false))
  })

  it('fetches config versions', async () => {
    const { result } = renderHook(() => useConfigVersions('svc', 'dev'), {
      wrapper,
    })
    await waitFor(() =>
      expect(result.current.data?.pages[0].versions[0].version).toBe(2)
    )
  })
})
