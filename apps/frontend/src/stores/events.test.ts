import { describe, it, expect, beforeEach } from 'vitest';
import { useEventsStore } from './events';

describe('useEventsStore', () => {
  beforeEach(() => {
    useEventsStore.setState({ events: [], max: 2 });
  });

  it('adds events up to max and clears', () => {
    const { add, clear } = useEventsStore.getState();
    add({ asset: 'A', type: 't1' });
    add({ asset: 'B', type: 't2' });
    add({ asset: 'C', type: 't3' });
    expect(useEventsStore.getState().events).toHaveLength(2);
    clear();
    expect(useEventsStore.getState().events).toHaveLength(0);
  });
});
