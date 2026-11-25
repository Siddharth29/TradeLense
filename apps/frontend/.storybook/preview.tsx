import type { Preview } from '@storybook/react';
import '../src/styles/globals.css';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { ThemeProvider } from '../src/app/theme-provider';
import { TooltipProvider } from '../src/components/ui/tooltip';

const preview: Preview = {
  parameters: {
    actions: { argTypesRegex: '^on[A-Z].*' },
    controls: {
      matchers: {
        color: /(background|color)$/i,
        date: /Date$/,
      },
    },
  },
  decorators: [
    (Story) => (
      <MemoryRouter>
        <ThemeProvider>
          <TooltipProvider>
            <Story />
          </TooltipProvider>
        </ThemeProvider>
      </MemoryRouter>
    ),
  ],
};

export default preview;
