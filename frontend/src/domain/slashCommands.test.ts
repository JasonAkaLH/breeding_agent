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
    display_name: '田间试验设计',
    description: '生成 RCBD 随机区组设计',
    version: '1',
    status: 'active',
    kind: 'skill',
    source: 'skill',
    source_path: 'mini_breedstat_rcbd_skill/SKILL.md',
  },
  {
    capability_id: 'skill.data_lookup',
    name: 'data-lookup',
    description: '只读数据库查询',
    version: '1',
    status: 'active',
    kind: 'skill',
    source: 'skill',
    source_path: 'data-lookup/SKILL.md',
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
      expect.objectContaining({ command: '/data-lookup', capabilityId: 'skill.data_lookup', hasCommandConflict: false }),
      expect.objectContaining({ command: '/mini-breedstat-rcbd', capabilityId: 'skill.mini_breedstat_rcbd', displayName: '田间试验设计', hasCommandConflict: false }),
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

  it('filters menu candidates by command, display name, name, description, and capability id', () => {
    const commands = deriveSlashCommands(capabilities);

    expect(slashMenuCandidates('/', commands).map((command) => command.command)).toEqual(['/data-lookup', '/mini-breedstat-rcbd']);
    expect(slashMenuCandidates('/data', commands).map((command) => command.command)).toEqual(['/data-lookup']);
    expect(slashMenuCandidates('/田间', commands).map((command) => command.command)).toEqual(['/mini-breedstat-rcbd']);
    expect(slashMenuCandidates('/随机', commands).map((command) => command.command)).toEqual(['/mini-breedstat-rcbd']);
    expect(slashMenuCandidates('/skill.data', commands).map((command) => command.command)).toEqual(['/data-lookup']);
  });

  it('parses exact slash command input into cleaned content and metadata', () => {
    const commands = deriveSlashCommands(capabilities);

    expect(parseDirectSlashCommand('/data-lookup 查询龙粳33', commands)).toEqual({
      kind: 'matched',
      command: expect.objectContaining({ command: '/data-lookup', capabilityId: 'skill.data_lookup' }),
      content: '查询龙粳33',
    });
    expect(slashSubmitIntent('/data-lookup 查询龙粳33', commands, null)).toEqual({
      kind: 'ready',
      content: '查询龙粳33',
      command: expect.objectContaining({ command: '/data-lookup', capabilityId: 'skill.data_lookup' }),
      metadata: { forced_by_slash_command: true, slash_command: '/data-lookup' },
    });
  });

  it('allows exact slash command with empty arguments and blocks unknown slash input', () => {
    const commands = deriveSlashCommands(capabilities);

    expect(slashSubmitIntent('/data-lookup', commands, null)).toEqual({
      kind: 'ready',
      content: '',
      command: expect.objectContaining({ command: '/data-lookup' }),
      metadata: { forced_by_slash_command: true, slash_command: '/data-lookup' },
    });
    expect(slashSubmitIntent('/unknown args', commands, null)).toEqual({ kind: 'blocked', reason: 'not_found', command: '/unknown' });
  });

  it('uses an explicitly selected badge before direct slash text', () => {
    const commands = deriveSlashCommands(capabilities);
    const selected = commands.find((command) => command.command === '/mini-breedstat-rcbd') ?? null;

    expect(slashSubmitIntent('/data-lookup 查询龙粳33', commands, selected)).toEqual({
      kind: 'ready',
      content: '/data-lookup 查询龙粳33',
      command: expect.objectContaining({ command: '/mini-breedstat-rcbd', capabilityId: 'skill.mini_breedstat_rcbd' }),
      metadata: { forced_by_slash_command: true, slash_command: '/mini-breedstat-rcbd' },
    });
  });
});
