import type { Meta, StoryObj } from '@storybook/react';
import { Topbar } from './topbar';
import { useSidebarStore } from '@/stores/sidebar';

const meta: Meta<typeof Topbar> = {
  title: 'Components/Topbar',
  component: Topbar,
};
export default meta;

type Story = StoryObj<typeof Topbar>;

export const Default: Story = {
  render: () => {
    useSidebarStore.setState({ toggle: () => {} });
    return <Topbar />;
  },
  parameters: { viewport: { defaultViewport: 'mobile1' } },
};
