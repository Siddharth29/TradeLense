import type { Meta, StoryObj } from '@storybook/react';
import { Logo, BrandMark, Wordmark } from './logo';

const meta: Meta<typeof Logo> = {
  title: 'Brand/Logo',
  component: Logo,
};
export default meta;

type Story = StoryObj<typeof Logo>;

/** The lockup at the sizes the app actually uses. */
export const Lockup: Story = {
  render: () => (
    <div className="flex flex-col gap-6 p-6">
      <Logo size={60} />
      <Logo size={32} />
      <Logo size={28} />
    </div>
  ),
};

/** Mark alone — auto-drops to the simplified icon below 28px, per the brand kit. */
export const Marks: Story = {
  render: () => (
    <div className="flex items-end gap-6 p-6">
      <BrandMark size={64} />
      <BrandMark size={40} />
      <BrandMark size={28} />
      <BrandMark size={20} />
      <BrandMark size={16} />
    </div>
  ),
};

/** Wordmark: one word, lowercase — "trade" muted, "lense" emerald. */
export const WordmarkOnly: Story = {
  render: () => (
    <div className="flex flex-col gap-4 p-6">
      <Wordmark size={48} />
      <Wordmark size={24} />
      <Wordmark size={15} />
    </div>
  ),
};
