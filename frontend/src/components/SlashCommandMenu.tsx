import { Tooltip } from 'antd';
import { useEffect, useRef } from 'react';
import type { MCPServerCommand } from '../domain/mcpServerCommands';
import type { SlashCommand } from '../domain/slashCommands';

type CommandCandidate = SlashCommand | MCPServerCommand;

interface SlashCommandMenuProps<T extends CommandCandidate> {
  candidates: T[];
  activeIndex: number;
  emptyMessage: string;
  variant?: 'skill' | 'mcp';
  onRefresh?(): void;
  onSelect(command: T): void;
}

export default function SlashCommandMenu<T extends CommandCandidate>({
  candidates,
  activeIndex,
  emptyMessage,
  variant = 'skill',
  onRefresh,
  onSelect,
}: SlashCommandMenuProps<T>) {
  const optionRefs = useRef<Array<HTMLDivElement | null>>([]);

  useEffect(() => {
    optionRefs.current[activeIndex]?.scrollIntoView?.({ block: 'nearest' });
  }, [activeIndex, candidates.length]);

  return (
    <div className="slash-command-menu-shell">
      {variant === 'mcp' && onRefresh ? (
        <button type="button" className="slash-command-refresh" onClick={onRefresh}>刷新 MCP Server</button>
      ) : null}
      <div className="slash-command-menu" role="listbox" aria-label={variant === 'mcp' ? 'MCP Server 命令列表' : 'Skill 命令列表'}>
        {candidates.length === 0 ? (
          <div className="slash-command-empty" role="status">{emptyMessage}</div>
        ) : candidates.map((candidate, index) => (
        <Tooltip
          key={`${candidateIdentity(candidate)}:${candidate.command}`}
          title={candidate.description}
          mouseEnterDelay={0.5}
          placement="right"
          classNames={{ root: 'slash-command-tooltip' }}
        >
          <div
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
              <span className="slash-command-title">{candidate.displayName}</span>
            </div>
            <div className="slash-command-description">{candidate.description}</div>
            {candidate.hasCommandConflict ? (
              <div className="slash-command-meta">
                {variant === 'mcp'
                  ? `名称冲突，请点选具体 Server · ${candidateIdentity(candidate)}`
                  : `命令冲突，请点选具体 capability · ${candidateIdentity(candidate)}`}
              </div>
            ) : null}
          </div>
        </Tooltip>
        ))}
      </div>
    </div>
  );
}

function candidateIdentity(candidate: CommandCandidate): string {
  return 'capabilityId' in candidate ? candidate.capabilityId : candidate.serverId;
}
