import type { MCPServerResponse } from '../api/types';

export interface MCPServerCommand {
  command: string;
  serverId: string;
  displayName: string;
  description: string;
  transport: string;
  hasCommandConflict: boolean;
}

export type DirectMCPServerParseResult =
  | { kind: 'matched'; command: MCPServerCommand; content: string }
  | { kind: 'not_found'; command: string }
  | { kind: 'conflict'; command: string }
  | { kind: 'not_mcp_server' };

export type MCPServerSubmitIntent =
  | {
      kind: 'ready';
      command: MCPServerCommand;
      content: string;
      capabilityId: 'mcp.dispatch';
      routingMode: 'force_capability';
      metadata: { mcp_server_binding: { server_id: string } };
    }
  | { kind: 'blocked'; reason: 'not_found' | 'conflict'; command: string }
  | { kind: 'auto'; content: string };

const MCP_SERVER_TOKEN_PATTERN = /^\$([^\s$]*)/u;
const LATIN_SCRIPT_PATTERN = /\p{Script=Latin}/gu;

export function deriveMCPServerCommands(servers: MCPServerResponse[]): MCPServerCommand[] {
  const commands = servers
    .filter((server) => server.enabled && server.health_status === 'available')
    .map((server) => ({
      command: `$${server.display_name}`,
      serverId: server.server_id,
      displayName: server.display_name,
      description: server.routing_description,
      transport: server.transport,
      hasCommandConflict: false,
    }))
    .sort((left, right) => left.displayName.localeCompare(right.displayName));

  const counts = new Map<string, number>();
  for (const command of commands) {
    const key = foldMCPCommand(command.command);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return commands.map((command) => ({
    ...command,
    hasCommandConflict: (counts.get(foldMCPCommand(command.command)) ?? 0) > 1,
  }));
}

export function mcpServerMenuCandidates(input: string, commands: MCPServerCommand[]): MCPServerCommand[] {
  const query = mcpServerQuery(input);
  if (query === null) return [];
  if (!query) return commands;
  const normalized = normalizeSearchText(query);
  return commands.filter((command) => {
    const haystack = [command.command, command.displayName, command.description, command.transport]
      .map(normalizeSearchText)
      .join(' ');
    return haystack.includes(normalized);
  });
}

export function parseDirectMCPServerCommand(
  input: string,
  commands: MCPServerCommand[],
): DirectMCPServerParseResult {
  const trimmed = input.trimStart();
  if (!trimmed.startsWith('$')) return { kind: 'not_mcp_server' };
  const match = trimmed.match(MCP_SERVER_TOKEN_PATTERN);
  const commandText = `$${match?.[1] ?? ''}`;
  const folded = foldMCPCommand(commandText);
  const matches = commands.filter((command) => foldMCPCommand(command.command) === folded);
  if (matches.length === 0) return { kind: 'not_found', command: commandText };
  if (matches.length > 1 || matches[0].hasCommandConflict) {
    return { kind: 'conflict', command: commandText };
  }
  return {
    kind: 'matched',
    command: matches[0],
    content: trimmed.slice(commandText.length).trimStart(),
  };
}

export function mcpServerSubmitIntent(
  input: string,
  commands: MCPServerCommand[],
  selected: MCPServerCommand | null,
): MCPServerSubmitIntent {
  const content = input.trim();
  if (selected) return readyIntent(selected, content);
  const parsed = parseDirectMCPServerCommand(input, commands);
  if (parsed.kind === 'matched') return readyIntent(parsed.command, parsed.content);
  if (parsed.kind === 'not_found') return { kind: 'blocked', reason: 'not_found', command: parsed.command };
  if (parsed.kind === 'conflict') return { kind: 'blocked', reason: 'conflict', command: parsed.command };
  return { kind: 'auto', content };
}

export function isMCPServerInput(input: string): boolean {
  return input.trimStart().startsWith('$');
}

function readyIntent(command: MCPServerCommand, content: string): MCPServerSubmitIntent {
  return {
    kind: 'ready',
    command,
    content,
    capabilityId: 'mcp.dispatch',
    routingMode: 'force_capability',
    metadata: { mcp_server_binding: { server_id: command.serverId } },
  };
}

function mcpServerQuery(input: string): string | null {
  const trimmed = input.trimStart();
  if (!trimmed.startsWith('$')) return null;
  return trimmed.slice(1).trim();
}

function foldMCPCommand(value: string): string {
  return value.normalize('NFC').replace(LATIN_SCRIPT_PATTERN, (character) => character.toLowerCase());
}

function normalizeSearchText(value: string): string {
  return foldMCPCommand(value).trim();
}
