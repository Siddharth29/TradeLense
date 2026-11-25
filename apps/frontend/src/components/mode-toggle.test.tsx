import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, beforeEach } from 'vitest';
import { ThemeProvider } from '@/app/theme-provider';
import { ModeToggle } from './mode-toggle';

describe('ModeToggle', () => {
  beforeEach(() => {
    document.documentElement.className = '';
    localStorage.clear();
  });

  it('toggles theme class on document', () => {
    render(
      <ThemeProvider>
        <ModeToggle />
      </ThemeProvider>
    );
    const btn = screen.getByRole('button', { name: /toggle theme/i });
    expect(document.documentElement.classList.contains('dark')).toBe(true);
    fireEvent.click(btn);
    expect(document.documentElement.classList.contains('light')).toBe(true);
  });
});
