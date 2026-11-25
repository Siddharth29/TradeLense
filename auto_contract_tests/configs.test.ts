import { beforeAll, afterAll, describe, it, expect } from 'vitest';
import { spawn, type ChildProcessWithoutNullStreams } from 'child_process';
import { setTimeout as delay } from 'node:timers/promises';

let proc: ChildProcessWithoutNullStreams;
const base = 'http://127.0.0.1:8002';

beforeAll(async () => {
  proc = spawn('python', ['-m', 'uvicorn', 'apps.backend.api:app', '--port', '8002'], {
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

describe('config endpoints', () => {
  it('saves and retrieves broker config', async () => {
    let res = await fetch(`${base}/configs/brokers/test`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        broker: 'mt5',
        config: { server: 's', loginId: 'l', password: 'p' },
      }),
    });
    expect(res.status).toBe(200);

    res = await fetch(`${base}/configs/brokers`);
    let data = await res.json();
    expect(data.mt5).toBeUndefined();

    res = await fetch(`${base}/configs/brokers`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mt5: { server: 's', loginId: 'l', password: 'p' },
      }),
    });
    expect(res.status).toBe(200);

    res = await fetch(`${base}/configs/brokers`);
    data = await res.json();
    expect(data.mt5.server).toBe('s');
    expect(data.mt5.loginId).toBe('l');
  });
});
