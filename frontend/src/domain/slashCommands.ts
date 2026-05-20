import type { CapabilityResponse } from '../api/types';

export interface SlashCommand {
  command: string;
  capabilityId: string;
  name: string;
  description: string;
  sourcePath: string;
  hasCommandConflict: boolean;
}

export type DirectSlashParseResult =
  | { kind: 'matched'; command: SlashCommand; content: string }
  | { kind: 'not_found'; command: string }
  | { kind: 'conflict'; command: string }
  | { kind: 'not_slash' };

export type SlashSubmitIntent =
  | { kind: 'ready'; command: SlashCommand; content: string; metadata: { forced_by_slash_command: true; slash_command: string } }
  | { kind: 'blocked'; reason: 'not_found' | 'conflict'; command: string }
  | { kind: 'auto'; content: string };

const SLASH_TOKEN_PATTERN = /^\/([^\s/]*)/;

export function deriveSlashCommands(capabilities: CapabilityResponse[]): SlashCommand[] {
  const activeSkills = capabilities
    .filter((capability) => capability.status === 'active' && capability.capability_id.startsWith('skill.'))
    .map((capability) => ({
      command: capabilityIdToSlashCommand(capability.capability_id),
      capabilityId: capability.capability_id,
      name: capability.name,
      description: capability.description,
      sourcePath: capability.source_path,
      hasCommandConflict: false,
    }))
    .sort((left, right) => left.command.localeCompare(right.command));

  const counts = new Map<string, number>();
  for (const command of activeSkills) {
    counts.set(command.command, (counts.get(command.command) ?? 0) + 1);
  }

  return activeSkills.map((command) => ({
    ...command,
    hasCommandConflict: (counts.get(command.command) ?? 0) > 1,
  }));
}

export function slashMenuCandidates(input: string, commands: SlashCommand[]): SlashCommand[] {
  const query = slashQuery(input);
  if (query === null) return [];
  if (!query) return commands;
  const normalized = normalizeSearchText(query);
  return commands.filter((command) => {
    const haystack = [command.command, command.name, command.description, command.capabilityId, command.sourcePath]
      .map(normalizeSearchText)
      .join(' ');
    return haystack.includes(normalized);
  });
}

export function parseDirectSlashCommand(input: string, commands: SlashCommand[]): DirectSlashParseResult {
  const trimmed = input.trimStart();
  if (!trimmed.startsWith('/')) return { kind: 'not_slash' };
  const match = trimmed.match(SLASH_TOKEN_PATTERN);
  const token = match?.[1] ?? '';
  const commandText = `/${token}`;
  const exactMatches = commands.filter((command) => command.command === commandText);
  if (exactMatches.length === 0) return { kind: 'not_found', command: commandText };
  if (exactMatches.length > 1 || exactMatches[0].hasCommandConflict) return { kind: 'conflict', command: commandText };
  const rest = trimmed.slice(commandText.length).trimStart();
  return { kind: 'matched', command: exactMatches[0], content: rest };
}

export function slashSubmitIntent(input: string, commands: SlashCommand[], selected: SlashCommand | null): SlashSubmitIntent {
  const content = input.trim();
  if (selected) {
    return readyIntent(selected, content);
  }
  const parsed = parseDirectSlashCommand(input, commands);
  if (parsed.kind === 'matched') return readyIntent(parsed.command, parsed.content);
  if (parsed.kind === 'not_found') return { kind: 'blocked', reason: 'not_found', command: parsed.command };
  if (parsed.kind === 'conflict') return { kind: 'blocked', reason: 'conflict', command: parsed.command };
  return { kind: 'auto', content };
}

export function isSlashInput(input: string): boolean {
  return input.trimStart().startsWith('/');
}

function readyIntent(command: SlashCommand, content: string): SlashSubmitIntent {
  return {
    kind: 'ready',
    content,
    command,
    metadata: {
      forced_by_slash_command: true,
      slash_command: command.command,
    },
  };
}

function slashQuery(input: string): string | null {
  const trimmed = input.trimStart();
  if (!trimmed.startsWith('/')) return null;
  const match = trimmed.match(SLASH_TOKEN_PATTERN);
  return match?.[1] ?? '';
}

function capabilityIdToSlashCommand(capabilityId: string): string {
  const suffix = capabilityId.replace(/^skill\./, '');
  const normalized = suffix
    .toLowerCase()
    .replace(/[_.]+/g, '-')
    .replace(/[^a-z0-9-]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');
  return `/${normalized || 'skill'}`;
}

function normalizeSearchText(value: string): string {
  return value.toLowerCase().replace(/[_.]+/g, '-').trim();
}
