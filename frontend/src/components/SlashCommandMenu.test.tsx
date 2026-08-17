import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import SlashCommandMenu from './SlashCommandMenu';
import type { SlashCommand } from '../domain/slashCommands';
import type { MCPServerCommand } from '../domain/mcpServerCommands';

function command(index: number): SlashCommand {
  return {
    command: `/skill-${index}`,
    capabilityId: `skill.skill_${index}`,
    name: `Skill ${index}`,
    displayName: `技能 ${index}`,
    description: `第 ${index} 个 Skill`,
    hasCommandConflict: false,
  };
}

describe('SlashCommandMenu', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders all commands inside one scrollable listbox while keeping active option visible', () => {
    const scrollIntoView = vi.fn();
    const originalScrollIntoView = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrollIntoView;
    try {
      render(
        <SlashCommandMenu
          candidates={[command(1), command(2), command(3), command(4)]}
          activeIndex={3}
          emptyMessage="未找到 Skill"
          onSelect={vi.fn()}
        />,
      );

      expect(screen.getByRole('listbox', { name: 'Skill 命令列表' })).toBeInTheDocument();
      expect(screen.getAllByRole('option')).toHaveLength(4);
      expect(screen.getByRole('option', { selected: true })).toHaveTextContent('/skill-4');
      expect(screen.getByRole('option', { selected: true })).toHaveTextContent('技能 4');
      expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest' });
    } finally {
      Element.prototype.scrollIntoView = originalScrollIntoView;
    }
  });

  it('shows the full description tooltip after the configured hover delay', async () => {
    const scrollIntoView = vi.fn();
    const originalScrollIntoView = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrollIntoView;
    try {
      render(
        <SlashCommandMenu
          candidates={[command(1)]}
          activeIndex={0}
          emptyMessage="未找到 Skill"
          onSelect={vi.fn()}
        />,
      );

      fireEvent.mouseEnter(screen.getByRole('option', { selected: true }));

      await waitFor(
        () => expect(screen.getByRole('tooltip')).toHaveTextContent('第 1 个 Skill'),
        { timeout: 900 },
      );
      expect(screen.getByRole('tooltip').closest('.ant-tooltip')).toHaveClass('slash-command-tooltip');
    } finally {
      Element.prototype.scrollIntoView = originalScrollIntoView;
    }
  });

  it('does not render Skill source paths in the option metadata', () => {
    const scrollIntoView = vi.fn();
    const originalScrollIntoView = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrollIntoView;
    try {
      render(
        <SlashCommandMenu
          candidates={[command(1)]}
          activeIndex={0}
          emptyMessage="未找到 Skill"
          onSelect={vi.fn()}
        />,
      );

      expect(screen.queryByText(/SKILL\.md/)).not.toBeInTheDocument();
      expect(screen.queryByText(/skill-1\/SKILL\.md/)).not.toBeInTheDocument();
    } finally {
      Element.prototype.scrollIntoView = originalScrollIntoView;
    }
  });

  it('renders the MCP variant with a separate refresh control and keyboard selection', () => {
    const onSelect = vi.fn();
    const onRefresh = vi.fn();
    const server: MCPServerCommand = {
      command: '$OCR服务',
      serverId: 'mcp-ocr',
      displayName: 'OCR服务',
      description: '识别图片',
      transport: 'streamable_http',
      hasCommandConflict: false,
    };
    render(
      <SlashCommandMenu
        candidates={[server]}
        activeIndex={0}
        emptyMessage="未找到 MCP Server"
        variant="mcp"
        onRefresh={onRefresh}
        onSelect={onSelect}
      />,
    );

    expect(screen.getByRole('listbox', { name: 'MCP Server 命令列表' })).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole('option'), { key: ' ' });
    expect(onSelect).toHaveBeenCalledWith(server);
    fireEvent.click(screen.getByRole('button', { name: '刷新 MCP Server' }));
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });
});
