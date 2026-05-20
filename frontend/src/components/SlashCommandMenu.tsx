import { useEffect, useRef } from 'react';
import type { SlashCommand } from '../domain/slashCommands';

interface SlashCommandMenuProps {
  candidates: SlashCommand[];
  activeIndex: number;
  emptyMessage: string;
  onSelect(command: SlashCommand): void;
}

export default function SlashCommandMenu({ candidates, activeIndex, emptyMessage, onSelect }: SlashCommandMenuProps) {
  const optionRefs = useRef<Array<HTMLDivElement | null>>([]);

  useEffect(() => {
    optionRefs.current[activeIndex]?.scrollIntoView?.({ block: 'nearest' });
  }, [activeIndex, candidates.length]);

  return (
    <div className="slash-command-menu" role="listbox" aria-label="Skill 命令列表">
      {candidates.length === 0 ? (
        <div className="slash-command-empty" role="status">{emptyMessage}</div>
      ) : candidates.map((candidate, index) => (
        <div
          key={`${candidate.capabilityId}:${candidate.command}`}
          ref={(element) => {
            optionRefs.current[index] = element;
          }}
          role="option"
          aria-selected={index === activeIndex}
          tabIndex={0}
          className={`slash-command-option${index === activeIndex ? ' slash-command-option-active' : ''}${candidate.hasCommandConflict ? ' slash-command-option-conflict' : ''}`}
          onClick={() => onSelect(candidate)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              onSelect(candidate);
            }
          }}
        >
          <div className="slash-command-option-main">
            <span className="slash-command-name">{candidate.command}</span>
            <span className="slash-command-title">{candidate.name}</span>
          </div>
          <div className="slash-command-description">{candidate.description}</div>
          {candidate.sourcePath || candidate.hasCommandConflict ? (
            <div className="slash-command-meta">
              {candidate.hasCommandConflict ? '命令冲突，请点选具体 capability · ' : ''}{candidate.capabilityId}{candidate.sourcePath ? ` · ${candidate.sourcePath}` : ''}
            </div>
          ) : null}
        </div>
      ))}
    </div>
  );
}
