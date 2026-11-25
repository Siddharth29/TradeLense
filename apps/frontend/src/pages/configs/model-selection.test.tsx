import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import ModelConfigPage from './model-selection';
import { getModelConfig, saveModelConfig } from '@/services/apiClient';

vi.mock('@/services/apiClient', () => ({
  getModelConfig: vi.fn(),
  saveModelConfig: vi.fn(),
}));

afterEach(cleanup);

it('saves Sonnet 5.5 reasoning effort and token budget from the model configuration page', async () => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  const original = { model: 'claude-sonnet-4-6', temperature: 0.2, top_p: 0.9, max_tokens: 4096 };
  vi.mocked(getModelConfig).mockResolvedValue({ config: original, checksum: null });
  vi.mocked(saveModelConfig).mockImplementation(async (config) => ({ status: 'ok', config }));

  render(<ModelConfigPage />);
  const model = await screen.findByRole('combobox', { name: 'Model' });
  fireEvent.keyDown(model, { key: 'Enter' });
  fireEvent.keyDown(await screen.findByRole('option', { name: 'Claude Sonnet 5.5' }), { key: 'Enter' });

  const effort = await screen.findByRole('combobox', { name: 'Reasoning Effort' });
  expect(effort).toHaveTextContent('Medium');
  expect(screen.queryByLabelText('Temperature')).not.toBeInTheDocument();
  expect(screen.queryByLabelText('Top P')).not.toBeInTheDocument();

  fireEvent.keyDown(effort, { key: 'Enter' });
  fireEvent.keyDown(await screen.findByRole('option', { name: 'High' }), { key: 'Enter' });
  fireEvent.change(screen.getByLabelText('Max Tokens'), { target: { value: '8192' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

  await waitFor(() => expect(saveModelConfig).toHaveBeenCalledWith({
    ...original,
    model: 'claude-sonnet-5-5',
    effort: 'high',
    max_tokens: 8192,
  }));
});
