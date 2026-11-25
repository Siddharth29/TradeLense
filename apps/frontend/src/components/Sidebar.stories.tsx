import type { Meta, StoryObj } from '@storybook/react';
import { Sidebar } from './sidebar';
import { useSidebarStore } from '@/stores/sidebar';

const meta: Meta<typeof Sidebar> = {
  title: 'Components/Sidebar',
  component: Sidebar,
  parameters: { layout: 'fullscreen' },
};
export default meta;

type Story = StoryObj<typeof Sidebar>;

export const Collapsed: Story = {
  render: () => {
    useSidebarStore.setState({ open: false });
    return <Sidebar />;
  },
};

export const Expanded: Story = {
  render: () => {
    useSidebarStore.setState({ open: true });
    return <Sidebar />;
  },
};
