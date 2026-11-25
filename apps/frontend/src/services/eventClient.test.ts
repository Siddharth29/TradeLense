import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.stubEnv('VITE_API_BASE_URL', 'http://api');

let lastWorker: MockWorker | undefined;
class MockWorker {
  postMessage: ReturnType<typeof vi.fn>;
  addEventListener: ReturnType<typeof vi.fn>;
  constructor() {
    this.postMessage = vi.fn();
    this.addEventListener = vi.fn();
    // eslint-disable-next-line @typescript-eslint/no-this-alias
    lastWorker = this;
  }
}
vi.stubGlobal('Worker', MockWorker as unknown as typeof Worker);

describe('eventClient subscribe', () => {
  let subscribe: (cb: () => void) => () => void;

  beforeEach(async () => {
    vi.resetModules();
    ({ subscribe } = await import('./eventClient'));
    lastWorker?.postMessage.mockClear();
  });

  it('starts worker on first subscriber and stops on last', () => {
    const unsub = subscribe(() => {});
    expect(lastWorker?.postMessage).toHaveBeenCalledWith({
      type: 'start',
      url: 'http://api/events',
    });
    unsub();
    expect(lastWorker?.postMessage).toHaveBeenCalledWith({ type: 'stop' });
  });
});
