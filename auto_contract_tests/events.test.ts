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

const readFrame = async (
  reader: ReadableStreamDefaultReader<Uint8Array>
): Promise<string> => {
  const dec = new TextDecoder();
  let buf = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) throw new Error('stream closed');
    buf += dec.decode(value, { stream: true });
    const norm = buf.replace(/\r\n/g, '\n');
    const idx = norm.indexOf('\n\n');
    if (idx !== -1) return norm.slice(0, idx);
  }
};

describe('event streams', () => {
  it('SSE /events', async () => {
    const res = await fetch(`${base}/events`);
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toMatch('text/event-stream');

    const reader = res.body!.getReader();
    const framePromise = readFrame(reader);
    await delay(50);
    await fetch(`${base}/events`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ asset: 'X', type: 'test' }),
    });
    const frame = await framePromise;
    expect(frame.startsWith('data: ')).toBe(true);
    const data = JSON.parse(frame.slice(6));
    expect(data.asset).toBe('X');
    await reader.cancel();
  });

  it('SSE /logs/stream', async () => {
    const res = await fetch(`${base}/logs/stream`);
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toMatch('text/event-stream');

    const reader = res.body!.getReader();
    const framePromise = readFrame(reader);
    await delay(50);
    await fetch(`${base}/logs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ msg: 'hello' }),
    });
    const frame = await framePromise;
    expect(frame.startsWith('data: ')).toBe(true);
    const data = JSON.parse(frame.slice(6));
    expect(data.msg).toBe('hello');
    await reader.cancel();
  });
});
