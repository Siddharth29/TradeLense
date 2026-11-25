import { beforeAll, afterAll, describe, it, expect } from 'vitest';
import { spawn, type ChildProcessWithoutNullStreams } from 'child_process';
import { setTimeout as delay } from 'node:timers/promises';

let proc: ChildProcessWithoutNullStreams;
const base = 'http://127.0.0.1:8001';

beforeAll(async () => {
  proc = spawn('python', ['-m', 'uvicorn', 'apps.backend.api:app', '--port', '8001'], {
    env: {
      ...process.env,
      SESSION_SECRET: 'testsecret',
      PYTHONPATH: './packages:./src',
    },
    stdio: 'ignore',
  });
  for (let i = 0; i < 20; i++) {
    try {
      await fetch(`${base}/health`);
      return;
    } catch {
      await delay(250);
    }
  }
  throw new Error('server did not start');
});

afterAll(() => {
  proc.kill();
});

describe('agent config endpoints', () => {
  it('round trips agent configs', async () => {
    let res = await fetch(`${base}/configs/agents`);
    expect(res.status).toBe(200);
    let data = await res.json();
    expect(data && typeof data === 'object' && !Array.isArray(data)).toBe(true);

    const payload = {
      agent_id: 'TEST',
      candles_5_count: 100,
      candles_15_count: 100,
      broker: 'mt5',
      gpt: { api_key: 'abc', model: 'gpt-3.5-turbo', temperature: 1 },
      risk: { daily_max_loss: 0, max_trades_per_day: 0, cooldown_minutes: 0, per_asset: {} },
      instrument: { symbol: 'AAPL', expiry: 'current_week' },
      admin: { env: 'dev', created_at: new Date().toISOString() },
    };

    res = await fetch(`${base}/configs/agents`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    expect(res.status).toBe(200);

    res = await fetch(`${base}/configs/agents`);
    data = await res.json();
    expect(data.agent_id).toBe('TEST');
    expect(data.broker).toBe('mt5');
    expect(data.candles_5_count).toBe(100);
    expect(data.candles_15_count).toBe(100);
  });
});
