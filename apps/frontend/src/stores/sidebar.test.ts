import { describe, it, expect, beforeEach } from 'vitest';
import { useSidebarStore } from './sidebar';

describe('useSidebarStore', () => {
  beforeEach(() => {
    useSidebarStore.setState({ open: false });
  });

  it('toggles and closes the sidebar', () => {
    const { toggle, close } = useSidebarStore.getState();
    expect(useSidebarStore.getState().open).toBe(false);
    toggle();
    expect(useSidebarStore.getState().open).toBe(true);
    close();
    expect(useSidebarStore.getState().open).toBe(false);
  });
});
