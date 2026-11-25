import { render, screen, fireEvent, act } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
vi.mock('react-window', () => ({
  List: ({ rowCount, rowComponent: Row }: any) => (
    <div>{Array.from({ length: rowCount }).map((_, i) => <Row key={i} index={i} style={{}} />)}</div>
  ),
}))

const now = new Date('2024-01-01T00:00:00Z').getTime()
const events = [
  { asset: 'AAPL', type: 'buy', timestamp: new Date(now - 60_000).toISOString() },
  { asset: 'MSFT', type: 'sell', timestamp: new Date(now - 120_000).toISOString() },
]

vi.mock('@/stores/events', () => ({
  useAgentEvents: () => events,
}))

import { EventTimeline } from './EventTimeline'

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(now)
})

afterEach(() => {
  vi.useRealTimers()
})

describe('EventTimeline', () => {
  it('filters events, shows relative time and allows pinning', () => {
    render(<EventTimeline />)
    act(() => {
      vi.advanceTimersByTime(200)
    })
    expect(screen.getByText('[AAPL] buy')).toBeInTheDocument()
    expect(screen.getByText('[MSFT] sell')).toBeInTheDocument()
    expect(screen.getByText('1m ago (IST)')).toBeInTheDocument()

    const pinBtn = screen.getAllByLabelText('Pin event')[0]
    fireEvent.click(pinBtn)
    expect(screen.getAllByLabelText('Unpin event')).toHaveLength(1)

    const assetInput = screen.getByPlaceholderText('Asset') as HTMLInputElement
    fireEvent.change(assetInput, { target: { value: 'AAPL' } })
    expect(screen.queryByText('[MSFT] sell')).toBeNull()

    const typeInput = screen.getByPlaceholderText('Event type') as HTMLInputElement
    fireEvent.change(typeInput, { target: { value: 'buy' } })
    expect(screen.getByText('[AAPL] buy')).toBeInTheDocument()
    fireEvent.change(typeInput, { target: { value: 'sell' } })
    expect(screen.queryByText('[AAPL] buy')).toBeNull()
  })
})
