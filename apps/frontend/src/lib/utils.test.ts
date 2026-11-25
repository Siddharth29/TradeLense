import { describe, it, expect } from 'vitest';
import { cn } from './utils';

describe('cn', () => {
  it('merges class names with tailwind overrides', () => {
    expect(cn('px-2', 'px-4')).toBe('px-4');
  });

  it('handles falsy values', () => {
    const hidden = false;
    expect(cn('px-2', undefined, hidden && 'hidden')).toBe('px-2');
  });
});
