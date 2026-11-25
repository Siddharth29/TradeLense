import { beforeAll, afterAll, describe, it, expect } from 'vitest';
import { spawn, type ChildProcessWithoutNullStreams } from 'child_process';
import { setTimeout as delay } from 'node:timers/promises';

let proc: ChildProcessWithoutNullStreams;
const base = 'http://127.0.0.1:8000';

beforeAll(async () => {
  proc = spawn('python', ['-m', 'uvicorn', 'apps.backend.api:app'], {
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

const isIso = (s: string) => !Number.isNaN(Date.parse(s));

describe('system endpoints', () => {
  it('GET /metrics', async () => {
    const res = await fetch(`${base}/metrics`);
    expect(res.status).toBe(200);
    const data = await res.json();
    expect(Array.isArray(data.equity_curve)).toBe(true);
    data.equity_curve.forEach((p: any) => {
      expect(isIso(p.time)).toBe(true);
      expect(typeof p.equity).toBe('number');
    });
    expect(Array.isArray(data.daily_pnl)).toBe(true);
    data.daily_pnl.forEach((p: any) => {
      expect(isIso(p.time)).toBe(true);
      expect(typeof p.pnl).toBe('number');
    });
    expect(typeof data.win_rate).toBe('number');
  });

  it('GET /health', async () => {
    const res = await fetch(`${base}/health`);
    expect(res.status).toBe(200);
    const data = await res.json();
    expect(isIso(data.time)).toBe(true);
    expect(typeof data.status).toBe('object');
  });

  it('GET /readiness', async () => {
    const res = await fetch(`${base}/readiness`);
    expect(res.status).toBe(200);
    const data = await res.json();
    expect(typeof data.ready).toBe('boolean');
    expect(typeof data.blockers).toBe('object');
    if (data.time) expect(isIso(data.time)).toBe(true);
  });

  it('GET/POST /flags', async () => {
    const setRes = await fetch(`${base}/flags/test`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value: true }),
    });
    expect(setRes.status).toBe(200);
    await setRes.json();

    const getRes = await fetch(`${base}/flags`);
    expect(getRes.status).toBe(200);
    const flags = await getRes.json();
    expect(typeof flags.test).toBe('boolean');
    expect(flags.test).toBe(true);
  });
});
