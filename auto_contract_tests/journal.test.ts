import { beforeAll, afterAll, describe, it, expect } from 'vitest'
import { spawn, type ChildProcessWithoutNullStreams } from 'child_process'
import { setTimeout as delay } from 'node:timers/promises'

let proc: ChildProcessWithoutNullStreams
const base = 'http://127.0.0.1:8002'

beforeAll(async () => {
  const trades = [
    { asset: 'NIFTY', time: '2024-01-01T00:00:00Z', type: 'open', agent_id: 'a1', prompt_hash: 'ph1' },
    { asset: 'NIFTY', time: '2024-01-01T00:05:00Z', type: 'break-even', agent_id: 'a1', prompt_hash: 'ph2' },
    { asset: 'NIFTY', time: '2024-01-01T00:10:00Z', type: 'closed', agent_id: 'a1', prompt_hash: 'ph3' },
  ]
  const candles = [
    { asset: 'NIFTY', time: '2024-01-01T00:00:00Z', open: 1, high: 2, low: 0.5, close: 1.5 },
    { asset: 'NIFTY', time: '2024-01-01T00:05:00Z', open: 1.5, high: 2.5, low: 1.3, close: 2.0 },
    { asset: 'NIFTY', time: '2024-01-01T00:10:00Z', open: 2.0, high: 2.3, low: 1.8, close: 2.2 },
  ]

  proc = spawn('python', ['-m', 'uvicorn', 'apps.backend.api:app', '--port', '8002'], {
    env: {
      ...process.env,
      SESSION_SECRET: 'testsecret',
      PYTHONPATH: './packages:./src',
      JOURNAL_TRADES_FIXTURE: JSON.stringify(trades),
      JOURNAL_CANDLES_FIXTURE: JSON.stringify(candles),
    },
    stdio: 'ignore',
  })
  for (let i = 0; i < 20; i++) {
    try {
      await fetch(`${base}/health`)
      return
    } catch {
      await delay(250)
    }
  }
  throw new Error('server did not start')
})

afterAll(() => {
  proc.kill()
})

describe('journal endpoints', () => {
  it('lists trades with pagination', async () => {
    let res = await fetch(`${base}/journal/list?limit=2`)
    expect(res.status).toBe(200)
    let data = await res.json()
    expect(data.entries).toHaveLength(2)
    expect(typeof data.nextCursor).toBe('string')

    res = await fetch(
      `${base}/journal/list?limit=2&cursor=${encodeURIComponent(data.nextCursor)}`
    )
    data = await res.json()
    expect(data.entries).toHaveLength(1)
    expect(data.nextCursor).toBeFalsy()
    expect(typeof data.entries[0].timestamp).toBe('string')
    expect(data.entries[0].agent_id).toBe('a1')
    expect(typeof data.entries[0].prompt_hash).toBe('string')
  })

  it('returns candles with pagination', async () => {
    let res = await fetch(`${base}/journal/NIFTY/candles?limit=2`)
    expect(res.status).toBe(200)
    let data = await res.json()
    expect(data.candles).toHaveLength(2)
    const cursor = data.nextCursor
    expect(typeof cursor).toBe('string')

    res = await fetch(
      `${base}/journal/NIFTY/candles?limit=2&cursor=${encodeURIComponent(cursor)}`
    )
    data = await res.json()
    expect(data.candles).toHaveLength(1)
    expect(data.nextCursor).toBeFalsy()
  })

  it('returns trades for asset', async () => {
    const res = await fetch(`${base}/journal/NIFTY/trades`)
    expect(res.status).toBe(200)
    const trades = await res.json()
    expect(trades).toHaveLength(3)
    expect(typeof trades[0].timestamp).toBe('string')
    expect(trades[0].agent_id).toBe('a1')
    expect(typeof trades[0].prompt_hash).toBe('string')
  })
})
