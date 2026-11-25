import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, it, expect } from 'vitest';
import { Breadcrumbs } from './breadcrumbs';

describe('Breadcrumbs', () => {
  it('renders links for each path segment', () => {
    render(
      <MemoryRouter initialEntries={['/agents']}>
        <Breadcrumbs />
      </MemoryRouter>
    );
    expect(screen.getByText('Home')).toBeInTheDocument();
    expect(screen.getByText('Agents')).toBeInTheDocument();
  });
});
