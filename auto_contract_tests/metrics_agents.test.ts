import { beforeAll, afterAll, describe, it, expect } from 'vitest';
import { spawn, type ChildProcessWithoutNullStreams } from 'child_process';
import { setTimeout as delay } from 'node:timers/promises';

let proc: ChildProcessWithoutNullStreams;
const base = 'http://127.0.0.1:8003';

beforeAll(async () => {
  proc = spawn('python', ['-m', 'uvicorn', 'apps.backend.api:app', '--port', '8003'], {
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

describe('agent metrics endpoint', () => {
  it('includes extended fields', async () => {
    const res = await fetch(`${base}/metrics/agents`);
    expect(res.status).toBe(200);
    const data = await res.json();
    expect(typeof data).toBe('object');
    Object.values(data).forEach((m: any) => {
      expect(typeof m.runs).toBe('number');
      expect(typeof m.decisions).toBe('number');
      expect(typeof m.orders).toBe('number');
      expect(typeof m.wins).toBe('number');
      expect(typeof m.losses).toBe('number');
      expect(typeof m.pnl).toBe('number');
      expect(typeof m.budget_skips).toBe('number');
      expect('latency_p50' in m).toBe(true);
      expect('latency_p95' in m).toBe(true);
      expect('last_run' in m).toBe(true);
      expect('eta' in m).toBe(true);
      expect('summary' in m).toBe(true);
      expect(typeof m.running).toBe('boolean');
      expect('usage' in m).toBe(true);
    });
  });
});
