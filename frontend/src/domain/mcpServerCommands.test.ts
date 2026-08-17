import { describe, expect, it } from 'vitest';
import type { MCPServerResponse } from '../api/types';
import {
  deriveMCPServerCommands,
  mcpServerMenuCandidates,
  mcpServerSubmitIntent,
  parseDirectMCPServerCommand,
} from './mcpServerCommands';

function server(overrides: Partial<MCPServerResponse> = {}): MCPServerResponse {
  return {
    server_id: 'mcp-1',
    display_name: 'OCR服务',
    routing_description: '识别图片和PDF',
    endpoint_url: 'https://secret.example.test',
    transport: 'streamable_http',
    protocol_preference: 'auto',
    auth_type: 'bearer',
    auth_metadata: { header_name: 'Authorization' },
    enabled: true,
    health_status: 'available',
    credential_configured: true,
    config_version: 1,
    security_version: 1,
    last_tested_at: null,
    last_test_error_code: null,
    created_at: '2026-08-17T00:00:00Z',
    updated_at: '2026-08-17T00:00:00Z',
    ...overrides,
  };
}

describe('MCP server commands', () => {
  it('derives only enabled and available safe command profiles', () => {
    const commands = deriveMCPServerCommands([
      server(),
      server({ server_id: 'disabled', display_name: 'Disabled', enabled: false }),
      server({ server_id: 'down', display_name: 'Down', health_status: 'unavailable' }),
    ]);

    expect(commands).toEqual([{
      command: '$OCR服务',
      serverId: 'mcp-1',
      displayName: 'OCR服务',
      description: '识别图片和PDF',
      transport: 'streamable_http',
      hasCommandConflict: false,
    }]);
    expect(JSON.stringify(commands)).not.toContain('secret.example.test');
    expect(JSON.stringify(commands)).not.toContain('Authorization');
  });

  it('matches Latin case including accented letters while preserving Unicode', () => {
    const commands = deriveMCPServerCommands([
      server({ server_id: 'accented', display_name: 'ÉLAN服务' }),
    ]);

    expect(parseDirectMCPServerCommand('$élan服务 执行', commands)).toEqual({
      kind: 'matched',
      command: commands[0],
      content: '执行',
    });
    expect(parseDirectMCPServerCommand('中间 $élan服务', commands)).toEqual({ kind: 'not_mcp_server' });
  });

  it('blocks normalized name conflicts and requires menu selection for names with spaces', () => {
    const conflicts = deriveMCPServerCommands([
      server({ server_id: 'one', display_name: 'CRM' }),
      server({ server_id: 'two', display_name: 'crm' }),
    ]);
    expect(parseDirectMCPServerCommand('$CrM 查询', conflicts)).toEqual({ kind: 'conflict', command: '$CrM' });

    const spaced = deriveMCPServerCommands([server({ server_id: 'space', display_name: 'My OCR' })]);
    expect(parseDirectMCPServerCommand('$My OCR', spaced)).toEqual({ kind: 'not_found', command: '$My' });
    expect(mcpServerSubmitIntent('处理文件', spaced, spaced[0])).toEqual({
      kind: 'ready',
      command: spaced[0],
      content: '处理文件',
      capabilityId: 'mcp.dispatch',
      routingMode: 'force_capability',
      metadata: { mcp_server_binding: { server_id: 'space' } },
    });
  });

  it('searches only command, name, description, and transport', () => {
    const commands = deriveMCPServerCommands([
      server({ server_id: 'ocr', display_name: 'OCR服务' }),
      server({ server_id: 'crm', display_name: 'CRM', routing_description: '客户订单', transport: 'legacy_http_sse' }),
    ]);

    expect(mcpServerMenuCandidates('$客户', commands).map((item) => item.serverId)).toEqual(['crm']);
    expect(mcpServerMenuCandidates('$legacy_http_sse', commands).map((item) => item.serverId)).toEqual(['crm']);
    expect(mcpServerMenuCandidates('$secret.example.test', commands)).toEqual([]);
  });
});
