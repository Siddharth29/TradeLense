import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { Sidebar } from '../sidebar';
import { useSidebarStore } from '@/stores/sidebar';

describe('Sidebar component', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useSidebarStore.setState({ open: true, close: useSidebarStore.getState().close });
  });

  it('renders navigation links when open', () => {
    render(
      <MemoryRouter>
        <Sidebar />
      </MemoryRouter>
    );
    expect(screen.getByText('Agents')).toBeInTheDocument();
  });

  it('calls close handler on click', () => {
    const close = vi.fn();
    useSidebarStore.setState({ open: true, close });
    const { container } = render(
      <MemoryRouter>
        <Sidebar />
      </MemoryRouter>
    );
    fireEvent.click(container.querySelector('aside')!);
    expect(close).toHaveBeenCalled();
  });
});
