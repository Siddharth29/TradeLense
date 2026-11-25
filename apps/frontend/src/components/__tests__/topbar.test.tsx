import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, it, expect, vi } from 'vitest';
import { Topbar } from '../topbar';
import { ThemeProvider } from '@/app/theme-provider';
import { useSidebarStore } from '@/stores/sidebar';

describe('Topbar', () => {
  it('shows environment label and triggers toggle', () => {
    const toggle = vi.fn();
    useSidebarStore.setState({ toggle });

    render(
      <MemoryRouter>
        <ThemeProvider>
          <Topbar />
        </ThemeProvider>
      </MemoryRouter>
    );

    expect(screen.getByText('dev')).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/toggle sidebar/i));
    expect(toggle).toHaveBeenCalled();
  });

  it('renders the TradeLense logo as a link home', () => {
    useSidebarStore.setState({ toggle: vi.fn() });

    render(
      <MemoryRouter>
        <ThemeProvider>
          <Topbar />
        </ThemeProvider>
      </MemoryRouter>
    );

    const home = screen.getByLabelText(/tradelense home/i);
    expect(home).toHaveAttribute('href', '/dashboard');
    // wordmark is live text: one word, lowercase
    expect(home).toHaveTextContent('tradelense');
  });
});
