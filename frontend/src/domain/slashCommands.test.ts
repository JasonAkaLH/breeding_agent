import { describe, expect, it } from 'vitest';
import type { CapabilityResponse } from '../api/types';
import {
  deriveSlashCommands,
  parseDirectSlashCommand,
  slashMenuCandidates,
  slashSubmitIntent,
} from './slashCommands';

const capabilities: CapabilityResponse[] = [
  {
    capability_id: 'main_agent.respond',
    name: '普通对话',
    description: 'Main agent',
    version: '1',
    status: 'active',
    kind: 'capability',
    source: 'builtin',
    source_path: '',
  },
  {
    capability_id: 'skill.mini_breedstat_rcbd',
    name: 'mini-breedstat-rcbd',
    description: '生成 RCBD 随机区组设计',
    version: '1',
    status: 'active',
    kind: 'skill',
    source: 'skill',
    source_path: 'mini_breedstat_rcbd_skill/SKILL.md',
  },
  {
    capability_id: 'skill.sql_query',
    name: 'sql-query',
    description: '只读数据库查询',
    version: '1',
    status: 'active',
    kind: 'skill',
    source: 'skill',
    source_path: 'sql-query/SKILL.md',
  },
  {
    capability_id: 'skill.disabled_demo',
    name: 'disabled-demo',
    description: 'disabled',
    version: '1',
    status: 'disabled',
    kind: 'skill',
    source: 'skill',
    source_path: 'disabled/SKILL.md',
  },
];

describe('slashCommands', () => {
  it('derives active skill commands from capabilities using stable capability ids', () => {
    const commands = deriveSlashCommands(capabilities);

    expect(commands).toEqual([
      expect.objectContaining({ command: '/mini-breedstat-rcbd', capabilityId: 'skill.mini_breedstat_rcbd', hasCommandConflict: false }),
      expect.objectContaining({ command: '/sql-query', capabilityId: 'skill.sql_query', hasCommandConflict: false }),
    ]);
    expect(commands.map((command) => command.capabilityId)).not.toContain('main_agent.respond');
    expect(commands.map((command) => command.capabilityId)).not.toContain('skill.disabled_demo');
  });

  it('marks normalized command collisions as direct-submit conflicts', () => {
    const commands = deriveSlashCommands([
      { ...capabilities[1], capability_id: 'skill.demo_query', name: 'demo-a' },
      { ...capabilities[1], capability_id: 'skill.demo-query', name: 'demo-b' },
    ]);

    expect(commands).toHaveLength(2);
    expect(commands.every((command) => command.command === '/demo-query')).toBe(true);
    expect(commands.every((command) => command.hasCommandConflict)).toBe(true);
    expect(parseDirectSlashCommand('/demo-query hello', commands)).toEqual({ kind: 'conflict', command: '/demo-query' });
  });

  it('filters menu candidates by command, name, description, and capability id', () => {
    const commands = deriveSlashCommands(capabilities);

    expect(slashMenuCandidates('/', commands).map((command) => command.command)).toEqual(['/mini-breedstat-rcbd', '/sql-query']);
    expect(slashMenuCandidates('/sql', commands).map((command) => command.command)).toEqual(['/sql-query']);
    expect(slashMenuCandidates('/随机', commands).map((command) => command.command)).toEqual(['/mini-breedstat-rcbd']);
    expect(slashMenuCandidates('/skill.sql', commands).map((command) => command.command)).toEqual(['/sql-query']);
  });

  it('parses exact slash command input into cleaned content and metadata', () => {
    const commands = deriveSlashCommands(capabilities);

    expect(parseDirectSlashCommand('/sql-query 查询龙粳33', commands)).toEqual({
      kind: 'matched',
      command: expect.objectContaining({ command: '/sql-query', capabilityId: 'skill.sql_query' }),
      content: '查询龙粳33',
    });
    expect(slashSubmitIntent('/sql-query 查询龙粳33', commands, null)).toEqual({
      kind: 'ready',
      content: '查询龙粳33',
      command: expect.objectContaining({ command: '/sql-query', capabilityId: 'skill.sql_query' }),
      metadata: { forced_by_slash_command: true, slash_command: '/sql-query' },
    });
  });

  it('allows exact slash command with empty arguments and blocks unknown slash input', () => {
    const commands = deriveSlashCommands(capabilities);

    expect(slashSubmitIntent('/sql-query', commands, null)).toEqual({
      kind: 'ready',
      content: '',
      command: expect.objectContaining({ command: '/sql-query' }),
      metadata: { forced_by_slash_command: true, slash_command: '/sql-query' },
    });
    expect(slashSubmitIntent('/unknown args', commands, null)).toEqual({ kind: 'blocked', reason: 'not_found', command: '/unknown' });
  });

  it('uses an explicitly selected badge before direct slash text', () => {
    const commands = deriveSlashCommands(capabilities);
    const selected = commands.find((command) => command.command === '/mini-breedstat-rcbd') ?? null;

    expect(slashSubmitIntent('/sql-query 查询龙粳33', commands, selected)).toEqual({
      kind: 'ready',
      content: '/sql-query 查询龙粳33',
      command: expect.objectContaining({ command: '/mini-breedstat-rcbd', capabilityId: 'skill.mini_breedstat_rcbd' }),
      metadata: { forced_by_slash_command: true, slash_command: '/mini-breedstat-rcbd' },
    });
  });
});
