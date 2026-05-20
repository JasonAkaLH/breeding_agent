import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import SlashCommandMenu from './SlashCommandMenu';
import type { SlashCommand } from '../domain/slashCommands';

function command(index: number): SlashCommand {
  return {
    command: `/skill-${index}`,
    capabilityId: `skill.skill_${index}`,
    name: `Skill ${index}`,
    description: `第 ${index} 个 Skill`,
    sourcePath: `skill-${index}/SKILL.md`,
    hasCommandConflict: false,
  };
}

describe('SlashCommandMenu', () => {
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
      expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest' });
    } finally {
      Element.prototype.scrollIntoView = originalScrollIntoView;
    }
  });
});
